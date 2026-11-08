"""Capture-owned whole-utterance classifier; unavailable is distinct from a veto."""

from pathlib import Path
import signal
import socket
import struct
import sys
import threading
import time

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from wake_verifier_process import (
    WakeVerifierProcess,
    STARTUP_SECONDS,
    READY,
    _receive,
    _set_remaining,
    classify_text,
)

MAX_COMPLETE_SAMPLES = 16000 * 12
COMPLETE_SECONDS = 1.0


class CompleteWakeVerifierProcess(WakeVerifierProcess):
    """One anonymous pipe, one in-flight decode, identity-checked invalidation.

    Capture stop can close a blocked decode. Its late error must never close a
    replacement worker. State locks never span inference or session callbacks.
    """

    def __init__(self):
        super().__init__(worker_script=Path(__file__).resolve())
        self._state = threading.RLock()
        self._decode = threading.Lock()

    def start(self):
        with self._state:
            super().start()

    @property
    def ready(self):
        with self._state:
            return super().ready

    def close(self):
        with self._state:
            super().close()

    def _invalidate(self, channel, process):
        with self._state:
            if self._channel is channel and self._process is process:
                super().close()

    def classify_complete(self, samples):
        if not self._decode.acquire(blocking=False):
            return None
        audio = None
        channel = process = None
        try:
            import numpy as np

            if (
                not isinstance(samples, np.ndarray)
                or samples.ndim != 1
                or samples.dtype.kind != 'f'
                or not 0 < len(samples) <= MAX_COMPLETE_SAMPLES
                or len(samples) % 160
                or not np.all(np.isfinite(samples))
            ):
                return None
            audio = np.array(samples, dtype='<f4', copy=True)
            with self._state:
                if not self.ready:
                    return None
                channel, process = self._channel, self._process
            deadline = time.monotonic() + COMPLETE_SECONDS
            channel.settimeout(COMPLETE_SECONDS)
            channel.sendall(struct.pack('>I', audio.nbytes))
            _set_remaining(channel, deadline)
            channel.sendall(memoryview(audio).cast('B'))
            _set_remaining(channel, deadline)
            reply = channel.recv(1)
            if len(reply) != 1 or reply[0] > 3:
                raise ValueError('Invalid complete wake response')
            with self._state:
                if self._channel is not channel or self._process is not process:
                    return None
                if process.poll() is not None:
                    self._invalidate(channel, process)
                    return None
            return bool(reply[0] & 1), bool(reply[0] & 2)
        except (OSError, EOFError, ValueError, TypeError):
            if channel is not None:
                self._invalidate(channel, process)
            return None
        finally:
            if audio is not None:
                audio.fill(0)
            self._decode.release()


def _worker(fd):
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
            if size % 640 or not 0 < size <= MAX_COMPLETE_SAMPLES * 4:
                raise ValueError('Invalid complete wake payload')
            pcm = _receive(channel, size)
            try:
                samples = np.frombuffer(pcm, dtype='<f4')
                if not np.all(np.isfinite(samples)):
                    raise ValueError('Invalid complete wake samples')
                signal.setitimer(signal.ITIMER_REAL, COMPLETE_SECONDS)
                target, confusable = classify_text(model.recognize(samples))
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                pcm[:] = bytes(size)
            channel.sendall(bytes((int(target) + 2 * int(confusable),)))


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        _worker(int(sys.argv[1]))
    except (EOFError, BrokenPipeError):
        pass

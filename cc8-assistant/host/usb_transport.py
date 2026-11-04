"""The USB stream owns one adb child and drains its stderr without retaining it.

TransportHub/Channel owns the returned stream lifetime. USB detection never
launches an Android UI or starts microphone/camera capture.
"""

import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]


class USBPipe:
    """Stock toybox netcat through the verified engineering shell prefix.

    This firmware rejects ADB's direct tcp service after registering a forward.
    The shell channel supports simultaneous raw stdin/stdout instead. No
    helper, root command, additional APK or system setting is involved.
    """

    def __init__(self, argv):
        self.child = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        self.write_timeout = 5
        self._closing = threading.Event()
        self._writer = threading.Lock()
        self._shutdown = threading.Lock()
        try:
            os.set_blocking(self.child.stdin.fileno(), False)
        except OSError:
            self.close()
            raise

        def drain():
            # Do not persist command output or private shell environment.
            try:
                while self.child.stderr.read(8192):
                    pass
            except (OSError, ValueError):
                pass

        threading.Thread(target=drain, daemon=True).start()

    def recv(self, count):
        if self._closing.is_set():
            return b''
        return os.read(self.child.stdout.fileno(), count)

    def sendall(self, data):
        with self._writer:
            if self._closing.is_set():
                raise EOFError('USB pipe closed')
            deadline = time.monotonic() + self.write_timeout
            view = memoryview(data)
            try:
                fd = self.child.stdin.fileno()
                while view:
                    if self._closing.is_set():
                        raise EOFError('USB pipe closed')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('USB pipe write timed out')
                    try:
                        sent = os.write(fd, view[:65536])
                    except (BlockingIOError, InterruptedError):
                        # Regular audio/control packets need no selector when
                        # the pipe is writable. Bound every backpressure wait.
                        with selectors.DefaultSelector() as poll:
                            poll.register(fd, selectors.EVENT_WRITE)
                            poll.select(min(0.05, remaining))
                        continue
                    if sent <= 0:
                        raise EOFError('USB pipe closed')
                    view = view[sent:]
            except (OSError, EOFError):
                # An incomplete CC8 packet must never be followed by another.
                self._closing.set()
                raise

    def shutdown(self, *unused):
        self._closing.set()
        # Terminating the child wakes blocking stdout reads. The Channel reader
        # still owns descriptor release, and a writer may be inside its poll.
        with self._shutdown:
            if self.child.poll() is not None:
                return
            self.child.terminate()
            try:
                self.child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.child.kill()
                try:
                    self.child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise OSError('USB child exit could not be confirmed') from None

    def close(self):
        try:
            self.shutdown()
        finally:
            with self._writer:
                for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
                    try:
                        stream.close()
                    except OSError:
                        pass

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def usb_target():
    sys.path.insert(0, str(ROOT / 'device-access'))
    import cc8_install as access

    # A network ADB endpoint must never be classified as a plugged USB cable.
    result = subprocess.run(
        [str(access.ADB), 'devices', '-l'], capture_output=True, text=True, check=True, timeout=5
    )
    matches = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if (
            len(parts) > 2
            and parts[1] == 'device'
            and ':' not in parts[0]
            and 'model:AILABS_S6' in parts
        ):
            match = re.search(r'transport_id:(\d+)', line)
            if match:
                matches.append(match.group(1))
    if len(matches) != 1:
        raise RuntimeError('No unique physical CC8 USB transport')
    return matches[0]


def usb_present():
    try:
        usb_target()
        return True
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False


def connect_device():
    sys.path.insert(0, str(ROOT / 'device-access'))
    import cc8_install as access

    transport = usb_target()
    return USBPipe(
        [
            str(access.ADB),
            '-t',
            transport,
            'shell',
            '-T',
            access.PREFIX + 'toybox nc -w 5 127.0.0.1 18765',
        ]
    )

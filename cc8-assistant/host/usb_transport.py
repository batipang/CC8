"""The USB stream owns one adb child and drains its stderr without retaining it.

TransportHub/Channel owns the returned stream lifetime. USB detection never
launches an Android UI or starts microphone/camera capture.
"""

import os
from pathlib import Path
import re
import subprocess
import sys
import threading

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
        self.error = None

        def drain():
            # Do not persist command output or private shell environment.
            while self.child.stderr.read(8192):
                pass

        threading.Thread(target=drain, daemon=True).start()

    def recv(self, count):
        return os.read(self.child.stdout.fileno(), count)

    def sendall(self, data):
        view = memoryview(data)
        while view:
            sent = os.write(self.child.stdin.fileno(), view)
            if sent <= 0:
                raise EOFError('USB pipe closed')
            view = view[sent:]

    def shutdown(self, *unused):
        self.close()

    def close(self):
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=2)
        for stream in [self.child.stdin, self.child.stdout, self.child.stderr]:
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

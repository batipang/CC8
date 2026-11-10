"""One owner-local metadata-only camera attempt; closing its socket cancels only this request."""

import json
import os
import re
import socket
import stat
import threading
import time
import uuid


class CameraProbe:
    def __init__(self, path, instance, generation, *, clock=time.monotonic):
        if not (
            isinstance(instance, str)
            and re.fullmatch('[a-f0-9]{32}', instance)
            and type(generation) is int
            and generation >= 0
        ):
            raise ValueError('camera_probe_context_invalid')
        self.path, self.clock = path, clock
        self.instance, self.generation = instance, generation
        self.ident = uuid.uuid4().hex
        self.done = threading.Event()
        self.stopped = threading.Event()
        self.result = None
        self.error = None
        self.connection = self.worker = None
        self.started = False

    def start(self):
        if self.started:
            raise RuntimeError('camera_probe_already_started')
        self.started = True
        info = self.path.lstat()
        if not (
            stat.S_ISSOCK(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
        ):
            raise RuntimeError('unsafe_camera_probe_socket')
        connection = self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.deadline = self.clock() + 20
        try:
            connection.settimeout(2)
            connection.connect(str(self.path))
            connection.sendall(
                (
                    json.dumps(
                        dict(
                            operation='camera_snapshot',
                            request_id=self.ident,
                            metadata_only=True,
                            expected_instance=self.instance,
                            expected_generation=self.generation,
                        )
                    )
                    + '\n'
                ).encode()
            )
            self.worker = threading.Thread(
                target=self.receive, daemon=True, name='cc8-camera-route-probe'
            )
            self.worker.start()
        except BaseException:
            self.close()
            raise

    def receive(self):
        connection = self.connection
        try:
            data = bytearray()
            while b'\n' not in data:
                if self.stopped.is_set():
                    raise OSError('camera_probe_closed')
                remaining = self.deadline - self.clock()
                if remaining <= 0:
                    raise ValueError('camera_probe_deadline')
                # Closing an fd from another thread need not wake macOS's
                # socket readiness wait. Bound each wait and observe own Stop.
                connection.settimeout(min(0.2, remaining))
                try:
                    block = connection.recv(min(4096, 4097 - len(data)))
                except socket.timeout:
                    continue
                if not block:
                    raise ValueError('camera_probe_response_missing')
                data.extend(block)
                if len(data) > 4096:
                    raise ValueError('camera_probe_response_oversized')
            value = json.loads(data)
            allowed = {
                'status',
                'request_id',
                'metadata_only',
                'reason',
                'media_type',
                'capture_version',
                'width',
                'height',
                'bytes',
                'sha256',
            }
            if not (
                isinstance(value, dict)
                and set(value) <= allowed
                and value.get('request_id') == self.ident
                and value.get('metadata_only') is True
                and isinstance(value.get('status'), str)
                and value['status'] in ('completed', 'interrupted', 'failed', 'timeout')
                and re.fullmatch('[a-f0-9]{32}', value['request_id'])
            ):
                raise ValueError('camera_probe_response_invalid')
            # No image, source text or raw failure message is retained/exported.
            self.result = dict(status=value['status'], request_id=self.ident, metadata_only=True)
        except (OSError, ValueError, TypeError, RecursionError):
            self.error = 'camera_probe_response_unconfirmed'
        finally:
            self.done.set()

    def close(self):
        self.stopped.set()
        connection = self.connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(1)
        if self.worker is not None and self.worker.is_alive():
            raise RuntimeError('camera_probe_reader_not_closed')

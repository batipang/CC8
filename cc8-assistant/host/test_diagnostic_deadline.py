"""Slow owner IPC cannot extend a read-only diagnostics call indefinitely."""

from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from diagnose import query


class DiagnosticDeadlineTests(unittest.TestCase):
    def test_drip_feed_has_one_overall_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'diagnostics.sock'
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            path.chmod(0o600)
            server.listen(1)
            server.settimeout(1)
            stopped = threading.Event()
            errors = []

            def drip():
                try:
                    with server.accept()[0] as peer:
                        peer.settimeout(1)
                        peer.recv(4096)
                        for byte in b'{"ok":true,"never_finishes":':
                            if stopped.wait(0.015):
                                break
                            peer.sendall(bytes([byte]))
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    if not stopped.is_set():
                        errors.append(type(error).__name__)

            worker = threading.Thread(target=drip, daemon=True)
            worker.start()
            try:
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    query(path, timeout=0.08)
                self.assertLess(time.monotonic() - started, 1)
            finally:
                stopped.set()
                server.close()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])

    def test_invalid_timeouts_fail_before_any_filesystem_or_socket_access(self):
        for timeout in (0, -1, True, float('nan'), float('inf'), 10**1000, 'secret'):
            with self.subTest(kind=type(timeout).__name__), patch.object(Path, 'lstat') as stat:
                with self.assertRaises(ValueError):
                    query(timeout=timeout)
                stat.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Closing a partial IPC startup must not remove someone else's path."""

from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from announce import AnnouncementServer


class AnnouncementSocketOwnershipTests(unittest.TestCase):
    def test_rejected_existing_file_survives_partial_startup_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'announce.sock'
            path.write_bytes(b'owner content')
            server = AnnouncementServer(Mock(), path)
            with self.assertRaises(RuntimeError):
                server.start()
            server.close()
            self.assertTrue(path.exists(), 'Cleanup removed a path it never bound')
            self.assertEqual(path.read_bytes(), b'owner content')

    def test_close_preserves_a_replacement_socket_at_the_same_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'announce.sock'
            server = AnnouncementServer(Mock(), path)
            with patch('announce.threading.Thread.start'):
                server.start()
            path.unlink()
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                replacement.bind(str(path))
                server.close()
                self.assertTrue(path.exists(), 'Cleanup removed a newer socket')
            finally:
                server.close()
                replacement.close()

    def test_normal_close_removes_only_its_bound_socket_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'announce.sock'
            server = AnnouncementServer(Mock(), path)
            with patch('announce.threading.Thread.start'):
                server.start()
            stream = server.socket
            self.assertTrue(path.exists())
            server.close()
            server.close()
            self.assertFalse(path.exists())
            self.assertEqual(stream.fileno(), -1)

    def test_close_between_loop_guard_and_accept_keeps_the_reader_on_its_socket(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'announce.sock'
            server = AnnouncementServer(Mock(), path)
            with patch('announce.threading.Thread.start'):
                server.start()
            stream = server.socket

            def concurrent_close():
                server.close()
                return False

            with patch.object(server.stop, 'is_set', side_effect=concurrent_close):
                server.run()
            self.assertEqual(stream.fileno(), -1)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()

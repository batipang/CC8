"""Real owner-private Unix IPC and CLI framing; fake native ACK, no media or cloud."""

import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock

from announce import AnnouncementServer
from identity_cli import request
import test_identity_runtime as fixture


class IdentityIpcTests(unittest.TestCase):
    def server(self):
        helper = fixture.IdentityRuntimeTests()
        b, channel = helper.session(saved_guest=True)
        self.addCleanup(helper.doCleanups)
        helper.ready(b)
        helper.ack(b)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'announce.sock'
        server = AnnouncementServer(b, path)
        server.start()
        self.addCleanup(server.close)
        return helper, b, channel, path

    def test_status_has_only_public_role_fields_and_owner_requires_actual_device_ack(self):
        helper, b, channel, path = self.server()
        status = request({'operation': 'identity_status'}, path)
        self.assertTrue(status['ok'])
        self.assertEqual(status['state']['mode'], 'guest')
        self.assertTrue(status['state']['device_applied'])
        threads = []
        original = channel.sendall

        def send(data):
            original(data)
            if channel.events[-1]['event'] == 'identity_state':
                worker = threading.Thread(target=helper.ack, args=(b,), daemon=True)
                threads.append(worker)
                worker.start()

        channel.sendall = send
        result = request({'operation': 'identity_owner', 'revision': 1}, path)
        for worker in threads:
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertTrue(result['ok'])
        self.assertEqual(result['state']['mode'], 'owner')
        self.assertTrue(result['state']['device_applied'])
        self.assertTrue(b.owner_access())
        self.assertFalse(b.session.active)

    def test_guest_socket_cannot_announce_read_owner_work_or_open_camera(self):
        _, b, _, path = self.server()
        b.announce = b.submit_text = b.request_snapshot = Mock(
            side_effect=AssertionError('Owner operation')
        )
        before = b.generation
        for payload in (
            {'text': 'OWNER_SECRET'},
            {'operation': 'text_request', 'text': 'OWNER_SECRET'},
            {'operation': 'camera_snapshot', 'request_id': 'a' * 32},
            {'operation': 'device_control', 'arguments': {'action': 'volume', 'percent': 50}},
        ):
            result = request(payload, path)
            self.assertEqual(result['status'], 'unavailable')
        b.announce.assert_not_called()
        self.assertEqual(b.generation, before)
        self.assertTrue(b.identity.guest)
        self.assertFalse(b.owner_access())

    def test_forged_extra_fields_stale_and_boolean_revision_do_not_change_role(self):
        _, b, _, path = self.server()
        before = b.identity.store.path.read_bytes()
        for payload in (
            {'operation': 'identity_owner', 'revision': 1, 'actor': 'owner'},
            {'operation': 'identity_owner', 'revision': True},
            {'operation': 'identity_owner', 'revision': 0},
            {'operation': 'identity_status', 'path': '/private/OWNER_SECRET'},
        ):
            result = request(payload, path)
            self.assertIn(result['status'], ('invalid_request', 'unavailable'))
        self.assertEqual(b.identity.store.path.read_bytes(), before)
        self.assertTrue(b.identity.guest)

    def test_duplicate_owner_revision_is_rejected_at_raw_socket_before_dispatch(self):
        _, b, _, path = self.server()
        before = b.identity.store.path.read_bytes()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            connection.sendall(b'{"operation":"identity_owner","revision":0,"revision":1}\n')
            result = json.loads(connection.recv(4096))
        self.assertEqual(result['status'], 'invalid_request')
        self.assertEqual(b.identity.store.path.read_bytes(), before)

    def test_cli_refuses_link_or_public_socket_before_connecting(self):
        _, _, _, path = self.server()
        alias = path.parent / 'alias.sock'
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            request({'operation': 'identity_status'}, alias)
        path.chmod(0o666)
        with self.assertRaises(OSError):
            request({'operation': 'identity_status'}, path)

"""Actual owner-private socket and camera IPC, using synthetic JPEGs only."""

import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from announce import AnnouncementServer
from camera_route_probe import CameraProbe
import test_desktop_camera as fixture


class MetadataBridge(fixture.IpcBridge):
    def health(self):
        return dict(
            runtime_instance='a' * 32,
            microphone_active=False,
            playing=False,
            calibrating=False,
            text_request_active=False,
        )

    def request_snapshot(self, *, metadata_only=False):
        assert metadata_only is True
        job = super().request_snapshot()
        job.metadata_only = True
        self.visual = SimpleNamespace(snapshot_job=job)
        return job


class CameraMetadataTests(unittest.TestCase):
    def test_route_side_effect_requires_the_same_live_metadata_preview_and_idle_audio(self):
        for fault in (
            None,
            'instance',
            'generation',
            'bool',
            'completed',
            'ordinary_photo',
            'missing',
            'microphone',
            'extra',
            'route',
        ):
            for route in ('wifi_handover', 'all_routes_lost'):
                with self.subTest(fault=fault, route=route):
                    bridge = MetadataBridge(False)
                    job = bridge.request_snapshot(metadata_only=True)
                    calls = []
                    bridge.transport_hub = SimpleNamespace(
                        suppress_usb_for_test=lambda seconds: (
                            calls.append(('usb', seconds)) or {'ok': True}
                        ),
                        suppress_all_for_test=lambda: calls.append(('all', 10)) or {'ok': True},
                    )
                    request = dict(
                        operation='camera_route_interrupt',
                        expected_instance='a' * 32,
                        expected_generation=100,
                        route=route,
                    )
                    if fault == 'instance':
                        request['expected_instance'] = 'b' * 32
                    elif fault == 'generation':
                        bridge.generation += 1
                    elif fault == 'bool':
                        request['expected_generation'] = True
                    elif fault == 'completed':
                        job.done.set()
                    elif fault == 'ordinary_photo':
                        job.metadata_only = False
                    elif fault == 'missing':
                        bridge.visual.snapshot_job = None
                    elif fault == 'microphone':
                        idle = bridge.health()
                        bridge.health = lambda: dict(idle, microphone_active=True)
                    elif fault == 'extra':
                        request['seconds'] = 60
                    elif fault == 'route':
                        request['route'] = 'unbounded'
                    server = AnnouncementServer(bridge, Path('/not-opened'))
                    if fault is None:
                        self.assertEqual(server.camera_route_interrupt(request), {'ok': True})
                        self.assertEqual(
                            calls, [('usb', 12)] if route == 'wifi_handover' else [('all', 10)]
                        )
                    else:
                        with self.assertRaises((RuntimeError, ValueError)):
                            server.camera_route_interrupt(request)
                        self.assertEqual(calls, [])

    def test_untrusted_response_never_retains_an_image_or_an_unmatched_receipt(self):
        for fault in ('image', 'nonce', 'flag', 'status', 'oversize', 'invalid_json', 'closed'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'probe.sock'
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(str(path))
                    os.chmod(path, 0o600)
                    server.listen(1)
                    server.settimeout(2)

                    def reply():
                        with server.accept()[0] as connection:
                            connection.settimeout(2)
                            request = bytearray()
                            while b'\n' not in request:
                                request.extend(connection.recv(4096))
                            value = dict(
                                status='interrupted',
                                request_id=json.loads(request)['request_id'],
                                metadata_only=True,
                            )
                            if fault == 'image':
                                value['image_base64'] = 'private_image'
                            elif fault == 'nonce':
                                value['request_id'] = 'f' * 32
                            elif fault == 'flag':
                                value['metadata_only'] = 1
                            elif fault == 'status':
                                value['status'] = []
                            raw = json.dumps(value).encode() + b'\n'
                            if fault == 'oversize':
                                raw = b'x' * 4097 + b'\n'
                            elif fault == 'invalid_json':
                                raw = b'{invalid\n'
                            if fault != 'closed':
                                connection.sendall(raw)

                    worker = threading.Thread(target=reply, daemon=True)
                    worker.start()
                    probe = CameraProbe(path, 'a' * 32, 1)
                    try:
                        probe.start()
                        self.assertTrue(probe.done.wait(2))
                        self.assertIsNone(probe.result)
                        self.assertEqual(probe.error, 'camera_probe_response_unconfirmed')
                    finally:
                        probe.close()
                        worker.join(2)
                        self.assertFalse(worker.is_alive())

    def test_actual_controller_metadata_mode_uses_visible_check_status_and_no_model_or_audio(self):
        bridge = fixture.SnapshotBridgeTests().bridge()
        statuses = []
        bridge.status = lambda *args: statuses.append(args)
        original = bridge.event

        def event(name, **fields):
            original(name, **fields)
            if name == 'camera_capture':
                for value in fixture.events(bridge.visual.inbox.pending):
                    bridge.on_event(value)

        bridge.event = event
        job = bridge.request_snapshot(metadata_only=True)
        self.assertTrue(job.done.wait(2))
        self.assertTrue(job.metadata_only)
        self.assertEqual(job.result, 'completed')
        self.assertIn(('thinking', '相机连接检查 · 正在取景'), statuses)
        self.assertFalse(bridge.session.active)
        self.assertFalse(any(name in ('play_begin', 'session_start') for name, _ in bridge.events))

    def test_metadata_only_never_encodes_or_returns_image_and_server_discards_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'camera.sock'
            bridge = MetadataBridge()
            server = AnnouncementServer(bridge, path)
            server.start()
            probe = CameraProbe(path, 'a' * 32, bridge.generation)
            try:
                with patch(
                    'announce.base64.b64encode', side_effect=AssertionError('No image export')
                ):
                    probe.start()
                    self.assertTrue(probe.done.wait(2))
                    self.assertIsNone(probe.error)
                    self.assertEqual(
                        probe.result,
                        dict(status='completed', request_id=probe.ident, metadata_only=True),
                    )
                self.assertIsNone(bridge.job.image)
            finally:
                probe.close()
                server.close()

    def test_closing_own_probe_cancels_only_its_pending_server_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'camera.sock'
            bridge = MetadataBridge(False)
            server = AnnouncementServer(bridge, path)
            server.start()
            probe = CameraProbe(path, 'a' * 32, bridge.generation)
            try:
                probe.start()
                # Server processing is asynchronous; the real socket is not
                # replaced with a fake completed receipt.
                deadline = time.monotonic() + 2
                while bridge.job is None and time.monotonic() < deadline:
                    threading.Event().wait(0.01)
                self.assertIsNotNone(bridge.job)
                probe.close()
                self.assertTrue(bridge.cancelled.wait(2))
                self.assertIsNone(bridge.job.image)
            finally:
                probe.close()
                server.close()

    def test_wrong_metadata_flag_or_extra_fields_rejected_before_capture(self):
        for extra in (
            dict(metadata_only=1),
            dict(metadata_only=False),
            dict(path='/tmp/other'),
            dict(text='unused'),
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'camera.sock'
                bridge = fixture.IpcBridge()
                server = AnnouncementServer(bridge, path)
                server.start()
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(2)
                        client.connect(str(path))
                        client.sendall(
                            (
                                json.dumps(
                                    dict(operation='camera_snapshot', request_id='a' * 32, **extra)
                                )
                                + '\n'
                            ).encode()
                        )
                        value = json.loads(client.recv(4096))
                    self.assertEqual(value['status'], 'invalid_request')
                    self.assertIsNone(bridge.job)
                finally:
                    server.close()

    def test_health_reports_only_camera_ownership_without_starting_a_request(self):
        b = fixture.SnapshotBridgeTests().bridge()
        health = b.health()
        self.assertTrue(health['camera_metadata_only_supported'])
        self.assertFalse(health['desktop_snapshot_active'] or health['vision_active'])
        self.assertIsNone(health['desktop_snapshot_generation'])
        self.assertEqual(health['desktop_snapshots_captured'], 0)
        self.assertIsNone(b.visual.snapshot_job)
        self.assertEqual(b.events, [])

    def test_stale_instance_or_generation_rejected_atomically_before_capture(self):
        for instance, generation in (('b' * 32, 100), ('a' * 32, 99)):
            with (
                self.subTest(instance=instance, generation=generation),
                tempfile.TemporaryDirectory() as folder,
            ):
                path = Path(folder) / 'camera.sock'
                bridge = MetadataBridge()
                server = AnnouncementServer(bridge, path)
                server.start()
                probe = CameraProbe(path, instance, generation)
                try:
                    probe.start()
                    self.assertTrue(probe.done.wait(2))
                    self.assertIsNotNone(probe.error)
                    self.assertIsNone(probe.result)
                    self.assertIsNone(bridge.job)
                finally:
                    probe.close()
                    server.close()

    def test_probe_constructor_rejects_unbound_or_coerced_context(self):
        for instance, generation in (('', 1), ('g' * 32, 1), ('a' * 32, True), ('a' * 32, -1)):
            with (
                self.subTest(instance=instance, generation=generation),
                self.assertRaises(ValueError),
            ):
                CameraProbe(Path('/not/opened'), instance, generation)

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from announce import AnnouncementServer
from bridge import Bridge, VisualReply
from camera import receive_photo, save_photo
from vision import CameraInbox, DesktopSnapshot
from test_vision import jpeg, events
from tools.java_toolchain import java_bin


class SnapshotBridgeTests(unittest.TestCase):
    def bridge(self):
        b = Bridge(None, None)
        b.connection = object()
        b.visual.challenge = 'c' * 32
        b.transport_hub = SimpleNamespace(pairing=SimpleNamespace(token='a' * 64))
        b.save_status = lambda: None
        b.events = []
        b.event = lambda name, **kw: b.events.append((name, kw))
        b.status = lambda *args: None
        return b

    def test_capture_returns_image_without_brain_tts_or_starting_microphone(self):
        b = self.bridge()
        original = b.event

        def event(name, **kw):
            original(name, **kw)
            if name == 'camera_capture':
                self.assertEqual(kw['origin'], 'desktop')
                c = b.visual.inbox.pending
                for value in events(c):
                    b.on_event(value)

        b.event = event
        job = b.request_snapshot()
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'completed')
        self.assertEqual(job.image, jpeg())
        self.assertFalse(b.active)
        self.assertIsNone(b.visual.snapshot_job)
        self.assertIsNone(b.visual.generation)
        self.assertIsNone(b.visual.inbox.pending)
        self.assertFalse(any(name in ('play_begin', 'session_start') for name, _ in b.events))

    def test_existing_microphone_remains_enabled(self):
        b = self.bridge()
        b.active = True

        def event(name, **kw):
            if name == 'camera_capture':
                for value in events(b.visual.inbox.pending):
                    b.on_event(value)

        b.event = event
        job = b.request_snapshot()
        self.assertTrue(job.done.wait(2))
        self.assertTrue(b.active)

    def test_camera_busy_calibration_and_disconnect_reject_new_request(self):
        for field, value in [
            ('visual.generation', 123),
            ('calibrating', True),
            ('connection', None),
        ]:
            b = self.bridge()
            setattr(b.visual if field.startswith('visual.') else b, field.split('.')[-1], value)
            with self.assertRaises(RuntimeError):
                b.request_snapshot()

    def test_cancel_pending_capture_returns_no_picture(self):
        b = self.bridge()
        job = b.request_snapshot()
        b.cancel_snapshot(job)
        self.assertTrue(job.done.wait(1))
        self.assertEqual(job.result, 'interrupted')
        self.assertIsNone(job.image)
        self.assertFalse(b.valid(job.generation))

    def test_old_cancel_cannot_abort_new_picture(self):
        b = self.bridge()
        old = b.request_snapshot()
        b.cancel_snapshot(old)
        new = b.request_snapshot()
        b.cancel_snapshot(old)
        self.assertIs(b.visual.snapshot_job, new)
        self.assertFalse(new.done.is_set())
        b.cancel_snapshot(new)

    def test_stale_visual_job_cannot_replace_new_inbox(self):
        b = self.bridge()
        old = b.generation
        b.cancel('new')
        current = b.visual.inbox.begin(b.generation)
        b.answer_visual(VisualReply(old, 'old'))
        self.assertIs(b.visual.inbox.pending, current)

    def test_old_cleanup_cannot_clear_current_inbox(self):
        inbox = CameraInbox()
        old = inbox.begin(1)
        current = inbox.begin(2)
        inbox.cancel_capture(old)
        self.assertIs(inbox.pending, current)
        self.assertFalse(current.done.is_set())


class IpcBridge:
    def __init__(self, complete=True):
        self.guard = threading.RLock()
        self.generation = 100
        self.job = None
        self.complete = complete
        self.cancelled = threading.Event()

    def request_snapshot(self):
        self.job = DesktopSnapshot(self.generation, 'f' * 32)
        if self.complete:
            self.job.image = jpeg()
            self.job.result = 'completed'
            self.job.done.set()
        return self.job

    def cancel_snapshot(self, job):
        job.result = 'interrupted'
        job.image = None
        job.done.set()
        self.cancelled.set()


class CameraSocketTests(unittest.TestCase):
    def test_private_ipc_returns_verified_image_and_discards_server_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'camera.sock'
            bridge = IpcBridge()
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                result, image = receive_photo(path)
                self.assertEqual(image, jpeg())
                self.assertEqual(result['status'], 'completed')
                self.assertNotIn('image_base64', result)
                self.assertIsNone(bridge.job.image)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            finally:
                server.close()

    def test_disconnecting_desktop_cancels_pending_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'camera.sock'
            bridge = IpcBridge(False)
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(path))
                    client.sendall(
                        b'{"operation":"camera_snapshot","request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n'
                    )
                    end = time.monotonic() + 1
                    while bridge.job is None and time.monotonic() < end:
                        time.sleep(0.01)
                    self.assertIsNotNone(bridge.job)
                self.assertTrue(bridge.cancelled.wait(1))
                self.assertIsNone(bridge.job.image)
            finally:
                server.close()

    def test_stale_completed_job_never_returns_old_image(self):
        class Stale(IpcBridge):
            def request_snapshot(self):
                job = super().request_snapshot()
                self.generation += 1
                return job

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'camera.sock'
            bridge = Stale()
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                result, image = receive_photo(path)
                self.assertEqual(result['status'], 'interrupted')
                self.assertIsNone(image)
                self.assertIsNone(bridge.job.image)
            finally:
                server.close()

    def test_client_rejects_wrong_request_hash_and_large_response(self):
        for bad in ('request', 'hash', 'oversize'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'bad.sock'
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(str(path))
                os.chmod(path, 0o600)
                server.listen(1)

                def handle():
                    with server.accept()[0] as c:
                        request = json.loads(c.recv(4096))
                        data = jpeg()
                        value = {
                            'status': 'completed',
                            'request_id': request['request_id'],
                            'media_type': 'image/jpeg',
                            'bytes': len(data),
                            'width': 640,
                            'height': 480,
                            'sha256': hashlib.sha256(data).hexdigest(),
                            'image_base64': base64.b64encode(data).decode(),
                        }
                        if bad == 'request':
                            value['request_id'] = 'wrong'
                        if bad == 'hash':
                            value['sha256'] = '0' * 64
                        if bad == 'oversize':
                            value['image_base64'] = 'A' * 530000
                        try:
                            c.sendall(json.dumps(value).encode() + b'\n')
                        except OSError:
                            pass

                worker = threading.Thread(target=handle, daemon=True)
                worker.start()
                try:
                    with self.assertRaises(ValueError):
                        receive_photo(path)
                finally:
                    server.close()
                    worker.join(1)

    def test_non_private_socket_is_rejected_without_connecting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bad.sock'
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            os.chmod(path, 0o666)
            try:
                with self.assertRaises(ValueError):
                    receive_photo(path)
            finally:
                server.close()


class PhotoArtifactTests(unittest.TestCase):
    def test_photo_is_private_and_each_capture_has_distinct_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'photos'
            one = save_photo(jpeg(), root)
            two = save_photo(jpeg(), root)
            self.assertNotEqual(one, two)
            self.assertEqual(one.read_bytes(), jpeg())
            self.assertEqual(stat.S_IMODE(one.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)

    def test_shared_or_symlink_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'photos'
            root.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                save_photo(jpeg(), root)
            link = Path(folder) / 'link'
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                save_photo(jpeg(), link)
            self.assertEqual(list(root.iterdir()), [])


class NativeDesktopAuthorizationTests(unittest.TestCase):
    def test_paired_signature_is_connection_bound_and_not_replayable(self):
        import subprocess
        from vision import desktop_signature

        root = Path(__file__).resolve().parents[2]
        java = java_bin()
        source = root / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
        harness = """package com.happyhome.cc8assistant;
public class DesktopAuthCheck {
 static void yes(boolean value){if(!value)throw new AssertionError();}
 public static void main(String[] args)throws Exception{
  String token="%s",nonce="%s";DesktopCameraAuth auth=new DesktopCameraAuth();
  String proof=TransportAuth.sign(token,"desktop-camera:"+auth.challenge()+":"+nonce+":100");
  yes(!auth.accept(token,nonce,100,"00"));yes(auth.accept(token,nonce,100,proof));
  yes(!auth.accept(token,nonce,100,proof));yes(!auth.accept(token,nonce,99,proof));
  auth.reset();yes(!auth.accept(token,nonce,100,proof));
  proof=TransportAuth.sign(token,"desktop-camera:"+auth.challenge()+":"+nonce+":100");yes(auth.accept(token,nonce,100,proof));
  System.out.print(TransportAuth.sign(token,"desktop-camera:%s:"+nonce+":101"));
 }}""" % ('a' * 64, 'b' * 32, 'c' * 32)
        with tempfile.TemporaryDirectory() as directory:
            test = Path(directory) / 'DesktopAuthCheck.java'
            test.write_text(harness)
            subprocess.run(
                [
                    str(java / 'javac'),
                    '-d',
                    directory,
                    str(source / 'TransportAuth.java'),
                    str(source / 'DesktopCameraAuth.java'),
                    str(test),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            result = subprocess.run(
                [
                    str(java / 'java'),
                    '-cp',
                    directory,
                    'com.happyhome.cc8assistant.DesktopAuthCheck',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, desktop_signature('a' * 64, 'c' * 32, 'b' * 32, 101))


if __name__ == '__main__':
    unittest.main()

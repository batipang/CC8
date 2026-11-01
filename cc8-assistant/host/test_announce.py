import json
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
import numpy as np

from announce import Announcement, AnnouncementServer, validate_text
from bridge import Bridge
from protocol import receive, SPEAKER, EVENT
from test_runtime import Fragmented


class AnnouncementTests(unittest.TestCase):
    def bridge(self, interrupt=False):
        class Speech:
            def cancel(self):
                pass

            def stream(self, text, current):
                yield np.ones(320, dtype=np.int16)

        b = Bridge(Speech(), None)
        b.save_status = lambda: None

        class Sink:
            def __init__(self):
                self.packets = []

            def sendall(self, data):
                p = receive(Fragmented(data))
                self.packets.append(p)
                if p.kind == SPEAKER and interrupt:
                    callback = threading.Thread(target=b.cancel, args=('test',), daemon=True)
                    callback.start()
                    callback.join(0.05)
                elif p.kind == EVENT and json.loads(p.data).get('event') == 'play_end':
                    b.on_event({'event': 'play_done', 'generation': p.generation})

        b.connection = Sink()
        return b

    def test_announcement_plays_without_starting_microphone(self):
        b = self.bridge()
        job = b.announce('常州是2026年苏超亚军！')
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'completed')
        self.assertFalse(b.active)
        events = [json.loads(p.data) for p in b.connection.packets if p.kind == EVENT]
        begin = next(e for e in events if e['event'] == 'play_begin')
        self.assertEqual(begin['role'], 'announcement')
        self.assertEqual(begin['text'], job.text)
        self.assertTrue(any(p.kind == SPEAKER for p in b.connection.packets))
        self.assertIsNone(b.announcement_generation)
        self.assertFalse(any(e['event'] == 'session_start' for e in events))

    def test_cancelled_announcement_cannot_resume_audio(self):
        b = self.bridge(interrupt=True)
        job = b.announce('测试')
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'interrupted')
        self.assertFalse(b.active)
        self.assertFalse(b.valid(job.generation))
        self.assertIsNone(b.announcement_generation)

    def test_microphone_state_is_preserved_if_already_active(self):
        b = self.bridge()
        b.active = True
        job = b.announce('测试')
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'completed')
        self.assertTrue(b.active)

    def test_disconnect_and_calibration_are_rejected(self):
        b = self.bridge()
        b.calibration.active = True
        with self.assertRaises(RuntimeError):
            b.announce('测试')
        b.calibration.active = False
        b.connection = None
        with self.assertRaises(RuntimeError):
            b.announce('测试')

    def test_bounded_plain_text_only(self):
        for text in ['', None, 'a' * 501, 'hello\x00']:
            with self.assertRaises(ValueError):
                validate_text(text)

    def test_local_socket_permissions_and_completion_reply(self):
        b = self.bridge()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'announce.sock'
            server = AnnouncementServer(b, path)
            server.start()
            try:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3)
                    client.connect(str(path))
                    client.sendall(json.dumps({'text': '测试'}).encode() + b'\n')
                    result = json.loads(client.recv(4096))
                    self.assertEqual(result['status'], 'completed')
                self.assertFalse(b.active)
            finally:
                server.close()
            self.assertFalse(path.exists())

    def test_live_device_status_is_not_blocked_by_a_running_announcement(self):
        job = Announcement(1, 'test')
        job.result = 'completed'
        entered = threading.Event()

        class BridgeStub:
            generation = 1

            def announce(self, text):
                entered.set()
                return job

            def control_device(self, action, percent=None):
                return {
                    'ok': True,
                    'transport': 'wifi',
                    'foreground': True,
                    'microphone_active': False,
                }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'announce.sock'
            server = AnnouncementServer(BridgeStub(), path)
            server.start()
            try:
                with (
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first,
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second,
                ):
                    first.settimeout(2)
                    first.connect(str(path))
                    first.sendall(b'{"text":"test"}\n')
                    self.assertTrue(entered.wait(1))
                    second.settimeout(1)
                    second.connect(str(path))
                    second.sendall(
                        b'{"operation":"device_control","arguments":{"action":"status"}}\n'
                    )
                    self.assertEqual(json.loads(second.recv(4096))['transport'], 'wifi')
                    job.done.set()
                    self.assertEqual(json.loads(first.recv(4096))['status'], 'completed')
            finally:
                job.done.set()
                server.close()


if __name__ == '__main__':
    unittest.main()

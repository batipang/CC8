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
from protocol import receive, SPEAKER, EVENT, Packet
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

        b.session.connection = Sink()
        return b

    def test_announcement_plays_without_starting_microphone(self):
        b = self.bridge()
        job = b.announce('常州是2026年苏超亚军！')
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'completed')
        self.assertFalse(b.session.active)
        events = [json.loads(p.data) for p in b.session.connection.packets if p.kind == EVENT]
        begin = next(e for e in events if e['event'] == 'play_begin')
        self.assertEqual(begin['role'], 'announcement')
        self.assertEqual(begin['text'], job.text)
        self.assertTrue(any(p.kind == SPEAKER for p in b.session.connection.packets))
        self.assertIsNone(b.announcements.generation)
        self.assertFalse(any(e['event'] == 'session_start' for e in events))
        row = b.diagnostics_report()['requests'][-1]
        self.assertNotIn('confirmed_playback_clauses', row)

    def test_two_clauses_require_matched_playout_and_framed_completion(self):
        for missing in (None, 'start', 'frames'):
            with self.subTest(missing=missing):
                b = self.bridge()
                b.playback.set_playout_capability(True)
                b.playback.set_completion_capability(True)
                begins = []

                def inject(name, generation, playback_id, **fields):
                    payload = dict(
                        event=name, generation=generation, playback_id=playback_id, **fields
                    )
                    b.on_packet(Packet(EVENT, b.session.epoch, data=json.dumps(payload).encode()))

                class Loopback:
                    def sendall(self, raw):
                        packet = receive(Fragmented(raw))
                        if packet.kind == SPEAKER:
                            current = begins[-1]
                            if len(begins) == 2:
                                self.progress(begins[0], packet)
                            if len(begins) == 1 or missing != 'start':
                                self.progress(current, packet)
                                self.progress(current, packet)
                        elif packet.kind == EVENT:
                            event = json.loads(packet.data)
                            if event['event'] == 'play_begin':
                                begins.append(event)
                            if event['event'] == 'play_end':
                                ident = begins[-1]['playback_id']
                                if len(begins) == 2:
                                    inject(
                                        'play_done', packet.generation, ident - 1, played_frames=320
                                    )
                                inject('play_done', packet.generation, ident, played_frames=319)
                                fields = (
                                    {}
                                    if len(begins) == 2 and missing == 'frames'
                                    else {'played_frames': 320}
                                )
                                inject('play_done', packet.generation, ident, **fields)
                                inject('play_done', packet.generation, ident, **fields)

                    def progress(self, begin, packet):
                        inject(
                            'playout_progress',
                            packet.generation,
                            begin['playback_id'],
                            first_signal_frame=0,
                            played_frames=160,
                            accepted_frames=320,
                            nanos=1,
                        )

                b.session.connection = Loopback()
                self.assertTrue(b.health()['clause_receipts_supported'])
                job = b.announce('测试' * 66)
                self.assertTrue(job.done.wait(2))
                self.assertEqual(job.result, 'completed')
                self.assertFalse(b.session.active)
                self.assertEqual([len(row['text']) for row in begins], [120, 12])
                self.assertEqual(len({row['playback_id'] for row in begins}), 2)
                rows = b.diagnostics_report()['requests']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['confirmed_playback_clauses'], 2 if missing is None else 1)

    def test_cancelled_announcement_cannot_resume_audio(self):
        b = self.bridge(interrupt=True)
        job = b.announce('测试')
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'interrupted')
        self.assertFalse(b.session.active)
        self.assertFalse(b.valid(job.generation))
        self.assertIsNone(b.announcements.generation)

    def test_microphone_state_is_preserved_if_already_active(self):
        b = self.bridge()
        b.session.active = True
        job = b.announce('测试')
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'completed')
        self.assertTrue(b.session.active)

    def test_disconnect_and_calibration_are_rejected(self):
        b = self.bridge()
        b.calibration.active = True
        with self.assertRaises(RuntimeError):
            b.announce('测试')
        b.calibration.active = False
        b.session.connection = None
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
                self.assertFalse(b.session.active)
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

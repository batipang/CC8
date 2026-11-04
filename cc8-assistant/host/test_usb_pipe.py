"""Exercise the transport's write deadline against real local child pipes."""

import hashlib
import socket
import sys
import threading
import unittest
from types import SimpleNamespace

from protocol import Packet
from release_info import RELEASE
from transport import TransportHub
from usb_transport import USBPipe


HELLO = Packet.event('transport_hello', protocol=RELEASE.protocol_version).encode()
LARGE_PACKET = b'x' * (2 * 1024**2)


class USBPipeTests(unittest.TestCase):
    def pipe(self, code, *args):
        pipe = USBPipe([sys.executable, '-I', '-S', '-c', code, *args])
        self.addCleanup(pipe.close)
        return pipe

    def peer(self, *, reject_writes=False):
        setup = 'os.close(0); ' if reject_writes else ''
        pipe = self.pipe(
            f'import os,sys,time; {setup}os.write(1, bytes.fromhex(sys.argv[1])); time.sleep(60)',
            HELLO.hex(),
        )
        pipe.write_timeout = 0.12
        return pipe

    def hub(self):
        hub = TransportHub(SimpleNamespace(device_config=lambda: {}), lambda: None, lambda: False)
        self.addCleanup(hub.close)
        return hub

    def wifi(self, hub):
        host, peer = socket.socketpair()
        self.addCleanup(peer.close)
        channel = hub.register(host, 'wifi')
        peer.sendall(HELLO)
        self.assertEqual(hub.events.get(timeout=2), ('up', channel, None))
        return channel

    def send_async(self, sender, data):
        done = threading.Event()
        errors = []

        def run():
            try:
                sender.sendall(data)
            except (OSError, EOFError) as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return done, errors, thread

    def test_backpressured_usb_closes_and_preserves_ready_wifi(self):
        pipe = self.peer()
        hub = self.hub()
        wifi = self.wifi(hub)
        usb = hub.register(pipe, 'usb')
        self.assertEqual(hub.events.get(timeout=2), ('up', usb, None))
        hub.selected(usb)

        done, errors, thread = self.send_async(usb, LARGE_PACKET)
        try:
            self.assertTrue(done.wait(2), 'USB write did not respect its budget')
            self.assertIsInstance(errors[0], TimeoutError)
            self.assertTrue(usb.closed)
            self.assertEqual(hub.events.get(timeout=2), ('down', usb, None))
            self.assertIs(hub.preferred(), wifi)
            hub.selected(wifi)
            self.assertIs(hub.active, wifi)
            self.assertFalse(wifi.closed)
        finally:
            usb.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_failed_activation_is_owned_by_usb_and_leaves_wifi_selectable(self):
        pipe = self.peer(reject_writes=True)
        hub = self.hub()
        wifi = self.wifi(hub)
        usb = hub.register(pipe, 'usb')
        self.assertEqual(hub.events.get(timeout=2), ('up', usb, None))
        try:
            hub.selected(usb)
        except (OSError, EOFError):
            self.fail('Activation leaked the broken USB write to the event dispatcher')
        self.assertTrue(usb.closed)
        self.assertIs(hub.preferred(), wifi)
        self.assertFalse(wifi.closed)

    def test_pipe_timeout_invalidates_direct_callers_and_does_not_append_bytes(self):
        pipe = self.peer()
        done, errors, thread = self.send_async(pipe, LARGE_PACKET)
        try:
            self.assertTrue(done.wait(2), 'Direct USB pipe write did not respect its budget')
            self.assertIsInstance(errors[0], TimeoutError)
            with self.assertRaises(EOFError):
                pipe.sendall(b'must not append to a partial packet')
        finally:
            pipe.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_close_interrupts_backpressure_before_a_long_write_deadline(self):
        pipe = self.peer()
        pipe.write_timeout = 30
        done, errors, thread = self.send_async(pipe, LARGE_PACKET)
        try:
            self.assertFalse(done.wait(0.1))
            pipe.shutdown()
            self.assertTrue(done.wait(2))
            self.assertIsInstance(errors[0], (OSError, EOFError))
            self.assertNotIsInstance(errors[0], TimeoutError)
        finally:
            pipe.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_healthy_pipe_delivers_one_complete_payload(self):
        pipe = self.pipe(
            'import hashlib, os; '
            f'data = os.fdopen(0, "rb").read({len(LARGE_PACKET)}); '
            'os.write(1, hashlib.sha256(data).hexdigest().encode())'
        )
        done, errors, thread = self.send_async(pipe, LARGE_PACKET)
        try:
            self.assertTrue(done.wait(3))
            self.assertEqual(errors, [])
            self.assertEqual(pipe.recv(64), hashlib.sha256(LARGE_PACKET).hexdigest().encode())
        finally:
            pipe.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())

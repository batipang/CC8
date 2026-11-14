"""Exercise authenticated USB byte transport with real TLS and a local child."""

from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from transport_pairing import Pairing
from usb_transport import connect_authenticated_usb


PEER = """
import hashlib
from pathlib import Path
import socket
import ssl
import sys
from protocol import Packet, decode_event, receive

cert, token, kind = sys.argv[1:]
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
ctx.maximum_version = ssl.TLSVersion.TLSv1_2
with ctx.wrap_socket(socket.socket(fileno=0)) as connection:
    expected = ssl.PEM_cert_to_DER_cert(Path(cert).read_text())
    if hashlib.sha256(connection.getpeercert(binary_form=True)).digest() != hashlib.sha256(expected).digest():
        raise SystemExit(3)
    if kind == 'missing':
        packet = Packet.event('transport_auth', token=token)
    else:
        packet = Packet.event('transport_auth', token=token, transport=kind)
    connection.sendall(packet.encode())
    welcome = receive(connection)
    assert decode_event(welcome.data) == {'event': 'transport_welcome'}
    connection.sendall(b'R')
    assert connection.recv(1) == b'W'
    connection.recv(1)
"""


class UsbTlsTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pairing = Pairing(root / 'pairing')

    def argv(self, kind, token=None, certificate=None):
        return [
            sys.executable,
            '-c',
            PEER,
            str(certificate or self.pairing.folder / 'server.pem'),
            token or self.pairing.token,
            kind,
        ]

    def test_authenticated_child_socket_passes_bytes_then_closes_and_reaps(self):
        channel = connect_authenticated_usb(self.argv('usb'), self.pairing)
        self.addCleanup(channel.close)
        self.assertEqual(channel.recv(1), b'R')
        channel.sendall(b'W')
        channel.shutdown(socket.SHUT_RDWR)
        channel.close()
        self.assertIsNotNone(channel.child.poll())

    def test_wrong_secret_and_other_route_labels_are_rejected_and_reaped(self):
        popen = subprocess.Popen
        for kind, token in (('usb', '0' * 64), ('wifi', None), ('missing', None)):
            children = []

            def open_child(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                return child

            with (
                self.subTest(kind=kind),
                patch('usb_transport.subprocess.Popen', side_effect=open_child),
                self.assertRaises(ValueError),
            ):
                connect_authenticated_usb(self.argv(kind, token), self.pairing)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())

    def test_plaintext_legacy_usb_cannot_complete_tls(self):
        with self.assertRaises((ssl.SSLError, EOFError)):
            connect_authenticated_usb(
                [sys.executable, '-c', 'import os; os.write(1, b"not a TLS session")'],
                self.pairing,
            )

    def test_untrusted_certificate_prevents_activation(self):
        another = Pairing(self.pairing.folder.parent / 'other')
        with self.assertRaises((OSError, EOFError)):
            connect_authenticated_usb(
                self.argv('usb', certificate=another.folder / 'server.pem'), self.pairing
            )

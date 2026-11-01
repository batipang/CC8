import hashlib
import json
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from transport import Pairing, TransportHub, signature, private_ipv4
from protocol import Packet, receive, SPEAKER, RENDER
from bridge import Bridge, usb_target


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.pairing = Pairing(Path(cls.temp.name) / 'pair')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def hub(self):
        def absent():
            raise RuntimeError('USB absent')

        hub = TransportHub(
            self.pairing, absent, lambda: False, bind='127.0.0.1', tls_port=0, discovery_port=0
        )
        hub.start()
        self.addCleanup(hub.close)
        return hub

    def connect(self, hub, token=None):
        # Match Android's explicit leaf pin, rather than treating this leaf as a CA.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        client = context.wrap_socket(socket.socket(), server_hostname='Bumblebee Local Bridge')
        self.addCleanup(client.close)
        client.settimeout(2)
        client.connect(hub.server.getsockname())
        self.assertEqual(
            hashlib.sha256(client.getpeercert(binary_form=True)).hexdigest(), self.pairing.pin
        )
        client.sendall(Packet.event('transport_auth', token=token or self.pairing.token).encode())
        return client

    def up(self, hub):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            kind, channel, packet = hub.events.get(timeout=2)
            if kind == 'up':
                return channel
        self.fail('No ready transport')

    def test_tls_authentication_rejects_unknown_peer_then_accepts_paired_peer(self):
        hub = self.hub()
        bad = self.connect(hub, '0' * 64)
        self.assertEqual(bad.recv(1), b'')
        self.assertIsNone(hub.preferred())
        self.assertEqual(hub.auth_failures, 1)
        malformed = self.connect(hub, '非法令牌')
        self.assertEqual(malformed.recv(1), b'')
        self.assertEqual(hub.auth_failures, 2)
        good = self.connect(hub)
        self.assertEqual(json.loads(receive(good).data)['event'], 'transport_welcome')
        good.sendall(Packet.event('transport_hello', protocol=2).encode())
        channel = self.up(hub)
        self.assertIs(hub.preferred(), channel)
        self.assertEqual(channel.kind, 'wifi')

    def test_usb_wins_and_same_authenticated_wifi_remains_available_on_unplug(self):
        hub = self.hub()
        client = self.connect(hub)
        receive(client)
        client.sendall(Packet.event('transport_hello', protocol=2).encode())
        wifi = self.up(hub)
        hub.selected(wifi)
        self.assertEqual(json.loads(receive(client).data)['event'], 'transport_activate')
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        usb = hub.register(a, 'usb')
        b.sendall(Packet.event('transport_hello', protocol=2).encode())
        self.assertIs(self.up(hub), usb)
        self.assertIs(hub.preferred(), usb)
        hub.selected(usb)
        self.assertEqual(json.loads(receive(client).data)['event'], 'transport_standby')
        self.assertEqual(json.loads(receive(b).data)['event'], 'transport_activate')
        config = json.loads(receive(b).data)
        self.assertEqual(config['event'], 'lan_config')
        self.assertEqual(config['config']['certificate_sha256'], self.pairing.pin)
        b.close()
        deadline = time.monotonic() + 2
        while not usb.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIs(hub.preferred(), wifi)
        hub.selected(wifi)
        self.assertEqual(json.loads(receive(client).data)['event'], 'transport_activate')
        self.assertEqual(hub.diagnostics()['transport'], 'wifi')
        self.assertEqual(hub.switches, 3)

    def test_signed_discovery_has_no_secret_and_rejects_tampering(self):
        hub = self.hub()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(client.close)
        client.settimeout(0.3)
        nonce = secrets.token_hex(16)
        request = {'type': 'cc8_discover', 'nonce': nonce, 'mac': '0' * 64}
        client.sendto(json.dumps(request).encode(), hub.discovery.getsockname())
        with self.assertRaises(socket.timeout):
            client.recvfrom(2048)
        request['mac'] = signature(self.pairing.token, 'discover:' + nonce)
        client.sendto(json.dumps(request).encode(), hub.discovery.getsockname())
        raw, _ = client.recvfrom(2048)
        offer = json.loads(raw)
        self.assertNotIn(self.pairing.token, raw.decode())
        self.assertEqual(offer['nonce'], nonce)
        self.assertEqual(
            offer['mac'],
            signature(
                self.pairing.token,
                'offer:' + nonce + ':' + offer['server_id'] + ':' + str(offer['port']),
            ),
        )
        changed = {**request, 'nonce': secrets.token_hex(16)}
        self.assertIsNone(self.pairing.offer(changed))

    def test_private_files_and_address_filter(self):
        self.assertEqual((self.pairing.folder / 'pairing.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.pairing.folder / 'server-key.pem').stat().st_mode & 0o777, 0o600)
        for value in ['172.16.0.5', '192.168.1.3', '10.1.2.3', '127.0.0.1']:
            self.assertTrue(private_ipv4(value))
        for value in ['8.8.8.8', '2001:4860:4860::8888', 'bad']:
            self.assertFalse(private_ipv4(value))

    def test_usb_detection_excludes_network_adb_and_ambiguous_devices(self):
        prefix = 'List of devices attached\n'
        with patch(
            'bridge.subprocess.run',
            return_value=subprocess.CompletedProcess(
                [], 0, prefix + '172.16.0.216:5555 device model:AILABS_S6 transport_id:4\n'
            ),
        ):
            with self.assertRaises(RuntimeError):
                usb_target()
        with patch(
            'bridge.subprocess.run',
            return_value=subprocess.CompletedProcess(
                [], 0, prefix + 'usb-device device model:AILABS_S6 transport_id:7\n'
            ),
        ):
            self.assertEqual(usb_target(), '7')


class FakeChannel:
    def __init__(self, kind):
        self.kind = kind
        self.sent = []

    def sendall(self, raw):
        self.sent.append(raw)


class HandoverTests(unittest.TestCase):
    def bridge(self):
        b = Bridge(None, None)
        b.save_status = lambda: None
        b.connection = FakeChannel('usb')
        b.transport_kind = 'usb'
        return b

    def test_existing_capture_epoch_is_preserved_and_old_audio_is_discarded(self):
        b = self.bridge()
        b.active = True
        b.epoch = 12
        b.playing = True
        old = b.generation
        new = FakeChannel('wifi')
        b.change_transport(new)
        self.assertTrue(b.active)
        self.assertEqual(b.epoch, 12)
        self.assertGreater(b.generation, old)
        self.assertEqual(b.capture_lookahead_ns, 120_000_000)
        b.send(Packet(SPEAKER, old, data=b'\0\0'))
        self.assertFalse(new.sent)
        b.event('caption_timing', generation=old, timings=[])
        self.assertFalse(new.sent)
        b.on_packet(Packet(RENDER, old, data=b'\0\0'))
        self.assertEqual(b.metrics['render_frames'], 0)

    def test_losing_all_routes_stops_capture_and_reconnect_does_not_enable_it(self):
        b = self.bridge()
        b.active = True
        closed = []

        class Aec:
            def close(self):
                closed.append(True)

        b.aec = Aec()
        b.change_transport(None)
        self.assertFalse(b.active)
        self.assertTrue(closed)
        b.change_transport(FakeChannel('wifi'))
        self.assertFalse(b.active)
        b.on_event(
            {'event': 'device_ready', 'capture_active': False, 'supports_playback_pause': True}
        )
        self.assertFalse(b.active)

    def test_rebind_of_same_user_enabled_epoch_does_not_start_new_capture(self):
        b = self.bridge()
        b.active = True
        b.epoch = 2
        old = b.generation
        b.on_event({'event': 'session_rebind', 'capture_active': True, 'epoch': 2})
        self.assertTrue(b.active)
        self.assertEqual(b.generation, old)


class RebindIdempotenceTests(unittest.TestCase):
    def test_start_and_rebind_for_same_epoch_cannot_duplicate_calibration(self):
        for order in [('session_rebind', 'session_start'), ('session_start', 'session_rebind')]:
            with self.subTest(order=order):
                b = Bridge(None, None)
                b.connection = FakeChannel('usb')
                b.save_status = lambda: None
                began = threading.Event()
                with (
                    patch('bridge.WebRtcEchoCanceller'),
                    patch.object(
                        b, 'calibrate', side_effect=lambda generation: began.set()
                    ) as calibrate,
                ):
                    for event in order:
                        b.on_event({'event': event, 'capture_active': True, 'epoch': 7})
                    self.assertTrue(began.wait(1))
                    self.assertEqual(calibrate.call_count, 1)
                    generation = b.generation
                    b.on_event({'event': 'session_start', 'epoch': 7})
                    self.assertEqual(b.generation, generation)
                    self.assertEqual(calibrate.call_count, 1)
                    b.on_event({'event': 'session_start', 'epoch': 8})
                    deadline = time.monotonic() + 1
                    while calibrate.call_count < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(calibrate.call_count, 2)
                    self.assertEqual(b.epoch, 8)

    def test_duplicate_rebind_during_calibration_starts_only_one_calibration(self):
        b = Bridge(None, None)
        b.connection = FakeChannel('wifi')
        b.active = True
        b.epoch = 3
        b.calibrating = True
        began = threading.Event()
        with patch.object(b, 'calibrate', side_effect=lambda generation: began.set()) as calibrate:
            value = {'event': 'session_rebind', 'capture_active': True, 'epoch': 3}
            b.on_event(value)
            b.on_event(value)
            self.assertTrue(began.wait(1))
            self.assertEqual(calibrate.call_count, 1)


class NativeAuthTests(unittest.TestCase):
    def test_java_hmac_and_constant_time_hex_validation(self):
        root = Path(__file__).resolve().parents[2]
        config = root / '.toolchains/cc8/paths.json'
        if not config.exists():
            self.skipTest('Local JDK unavailable')
        java = Path(json.loads(config.read_text())['java_home']) / 'bin'
        source = (
            root / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/TransportAuth.java'
        )
        token = '11' * 32
        nonce = '22' * 16
        expected = signature(token, 'discover:' + nonce)
        harness = """package com.happyhome.cc8assistant;
public class AuthCheck {public static void main(String[] args)throws Exception{
 String got=TransportAuth.sign("%s","discover:%s");if(!got.equals("%s"))throw new AssertionError();
 if(!TransportAuth.matches(got,"%s")||TransportAuth.matches(got,"00")||TransportAuth.matches(got,"zz"))throw new AssertionError();
}}""" % (token, nonce, expected, expected)
        with tempfile.TemporaryDirectory() as folder:
            test = Path(folder) / 'AuthCheck.java'
            test.write_text(harness)
            result = subprocess.run(
                [str(java / 'javac'), '-d', folder, str(source), str(test)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [str(java / 'java'), '-cp', folder, 'com.happyhome.cc8assistant.AuthCheck'],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

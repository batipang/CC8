import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import queue
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from transport import Pairing, TransportHub, Channel, signature, private_ipv4
from protocol import Packet, receive, decode_event, SPEAKER, RENDER, EVENT, MIC
from bridge import Bridge, usb_target
from tools.java_toolchain import java_bin


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.pairing = Pairing(Path(cls.temp.name) / 'pair')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def hub(self, usb_present=lambda: False):
        def absent():
            raise RuntimeError('USB absent')

        hub = TransportHub(
            self.pairing, absent, usb_present, bind='127.0.0.1', tls_port=0, discovery_port=0
        )
        hub.start()
        self.addCleanup(hub.close)
        return hub

    def connect(self, hub, token=None, auth_data=None):
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
        client.sendall(
            (
                Packet(EVENT, data=auth_data)
                if auth_data is not None
                else Packet.event('transport_auth', token=token or self.pairing.token)
            ).encode()
        )
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
        present = threading.Event()
        present.set()
        hub = self.hub(present.is_set)

        def event(peer):
            for _ in range(8):
                value = json.loads(receive(peer).data)
                if value.get('event') != 'transport_ping':
                    return value
                peer.sendall(Packet.event('transport_pong').encode())
            self.fail('Expected control event was not delivered')

        client = self.connect(hub)
        receive(client)
        client.sendall(Packet.event('transport_hello', protocol=2).encode())
        wifi = self.up(hub)
        hub.selected(wifi)
        self.assertEqual(event(client)['event'], 'transport_activate')
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        usb = hub.register(a, 'usb')
        b.sendall(Packet.event('transport_hello', protocol=2).encode())
        self.assertIs(self.up(hub), usb)
        self.assertIs(hub.preferred(), usb)
        hub.selected(usb)
        self.assertEqual(event(client)['event'], 'transport_standby')
        self.assertEqual(event(b)['event'], 'transport_activate')
        config = event(b)
        self.assertEqual(config['event'], 'lan_config')
        self.assertEqual(config['config']['certificate_sha256'], self.pairing.pin)
        present.clear()
        b.close()
        deadline = time.monotonic() + 2
        while not usb.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIs(hub.preferred(), wifi)
        hub.selected(wifi)
        self.assertEqual(event(client)['event'], 'transport_activate')
        self.assertEqual(hub.diagnostics()['transport'], 'wifi')
        self.assertEqual(hub.switches, 3)

    def test_test_outage_closes_both_routes_rejects_registration_and_expires(self):
        hub = self.hub()
        wifi_peer = self.connect(hub)
        receive(wifi_peer)
        wifi_peer.sendall(Packet.event('transport_hello', protocol=2).encode())
        wifi = self.up(hub)
        usb_stream, usb_peer = socket.socketpair()
        self.addCleanup(usb_peer.close)
        usb = hub.register(usb_stream, 'usb')
        usb_peer.sendall(Packet.event('transport_hello', protocol=2).encode())
        self.assertIs(self.up(hub), usb)
        self.assertIs(hub.preferred(), usb)
        self.assertEqual(hub.suppress_all_for_test(), {'ok': True, 'seconds': 10})
        deadline = hub.all_disabled_until
        with self.assertRaises(RuntimeError):
            hub.suppress_all_for_test()
        with self.assertRaises(RuntimeError):
            hub.suppress_usb_for_test(12)
        self.assertEqual(hub.all_disabled_until, deadline)
        self.assertTrue(wifi.closed and usb.closed)
        self.assertIsNone(hub.preferred())
        self.assertFalse(hub.selected(usb))
        self.assertTrue(hub.diagnostics()['all_routes_test_suppressed'])
        self.assertTrue(hub.diagnostics()['usb_test_suppressed'])
        for kind in ('usb', 'wifi'):
            stream, peer = socket.socketpair()
            with peer, self.assertRaises(RuntimeError):
                hub.register(stream, kind)
            self.assertEqual(stream.fileno(), -1)
        with hub.guard:
            hub.all_disabled_until = time.monotonic() - 1
        peer = self.connect(hub)
        receive(peer)
        peer.sendall(Packet.event('transport_hello', protocol=2).encode())
        self.assertIs(self.up(hub), hub.preferred())
        self.assertFalse(hub.diagnostics()['all_routes_test_suppressed'])

    def test_deep_discovery_cannot_kill_listener(self):
        hub = self.hub()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(client.close)
        client.settimeout(1)
        client.sendto(b'[' * 1000 + b'0' + b']' * 1000, hub.discovery.getsockname())
        nonce = secrets.token_hex(16)
        request = {
            'type': 'cc8_discover',
            'nonce': nonce,
            'mac': signature(self.pairing.token, 'discover:' + nonce),
        }
        client.sendto(json.dumps(request).encode(), hub.discovery.getsockname())
        offer = json.loads(client.recvfrom(2048)[0])
        self.assertEqual(offer['nonce'], nonce)
        self.assertTrue(all(worker.is_alive() for worker in hub.workers))

    def test_ambiguous_auth_is_rejected_and_listener_recovers(self):
        hub = self.hub()
        bad = self.connect(
            hub,
            auth_data=(
                '{"event":"transport_auth","token":"bad","token":"' + self.pairing.token + '"}'
            ).encode(),
        )
        self.assertEqual(bad.recv(1), b'')
        self.assertEqual(hub.auth_failures, 1)
        good = self.connect(hub)
        self.assertEqual(json.loads(receive(good).data)['event'], 'transport_welcome')
        good.sendall(Packet.event('transport_hello', protocol=2).encode())
        self.assertIs(self.up(hub), hub.preferred())

    def test_bad_hello_versions_do_not_become_ready(self):
        hub = self.hub()
        for version in (True, 1, '2', 3, None):
            client = self.connect(hub)
            receive(client)
            client.sendall(Packet.event('transport_hello', protocol=version).encode())
            self.assertEqual(client.recv(1), b'')
            self.assertIsNone(hub.preferred())
        good = self.connect(hub)
        receive(good)
        good.sendall(Packet.event('transport_hello', protocol=2).encode())
        self.assertIs(self.up(hub), hub.preferred())

    def test_queue_saturation_closes_reader_instead_of_hanging(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        events = queue.Queue(maxsize=1)
        channel = Channel(left, 'usb', events)
        worker = threading.Thread(target=channel.read, daemon=True)
        worker.start()
        right.sendall(
            Packet.event('transport_hello', protocol=2).encode()
            + Packet.event('screen_touch').encode()
        )
        worker.join(1.5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(channel.closed)
        self.assertFalse(channel.ready)
        self.assertEqual(events.qsize(), 1)

    def test_concurrent_authenticated_replacements_stay_bounded_and_close(self):
        hub = self.hub()

        def connect():
            client = self.connect(hub)
            self.assertEqual(json.loads(receive(client).data)['event'], 'transport_welcome')

        # Stay within the listen(4) OS backlog; overload may legitimately be refused.
        for _ in range(20):
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(connect) for _ in range(3)]
                for future in futures:
                    future.result(timeout=8)
        self.assertEqual(len(hub.channels), 1)
        self.assertEqual(hub.auth_failures, 0)
        current = self.connect(hub)
        receive(current)
        current.sendall(Packet.event('transport_hello', protocol=2).encode())
        self.assertIs(self.up(hub), hub.preferred())
        hub.close()
        self.assertEqual(current.recv(1), b'')
        self.assertIsNone(hub.preferred())

    def test_foreign_close_wakes_reader_but_defers_descriptor_release(self):
        entered, released = threading.Event(), threading.Event()

        class BlockingStream:
            def __init__(self):
                self.finished = False
                self.shutdown_called = False

            def recv(self, count):
                entered.set()
                released.wait(2)
                return b''

            def shutdown(self, how):
                self.shutdown_called = True

            def close(self):
                self.finished = True

        stream = BlockingStream()
        channel = Channel(stream, 'wifi', queue.Queue())
        channel.start()
        try:
            self.assertTrue(entered.wait(1))
            channel.close()
            self.assertTrue(stream.shutdown_called)
            self.assertFalse(stream.finished)
            self.assertTrue(channel.closed)
        finally:
            released.set()
            channel.reader.join(2)
        self.assertFalse(channel.reader.is_alive())
        self.assertTrue(stream.finished)

    def test_close_during_incomplete_tls_handshake_is_bounded(self):
        hub = self.hub()
        client = socket.create_connection(hub.server.getsockname(), timeout=1)
        self.addCleanup(client.close)
        # Let accept() enter the bounded 3-second TLS handshake, without sending a ClientHello.
        time.sleep(0.05)
        hub.close()
        deadline = time.monotonic() + 4
        for worker in hub.workers:
            worker.join(max(0, deadline - time.monotonic()))
        self.assertFalse(any(worker.is_alive() for worker in hub.workers))
        with self.assertRaises(RuntimeError):
            hub.start()

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
        self.assertEqual(offer['port'], hub.server.getsockname()[1])
        self.assertEqual(self.pairing.device_config()['port'], hub.server.getsockname()[1])
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

    def test_pairing_json_boundary_rejects_wrong_types_duplicates_and_size(self):
        payloads = (
            json.dumps({'server_id': 7, 'token': self.pairing.token}).encode(),
            b'{"server_id":"x","server_id":"y","token":"z"}',
            b' ' * 4097,
        )
        for payload in payloads:
            with tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                for name in ('server.pem', 'server-key.pem'):
                    shutil.copyfile(self.pairing.folder / name, directory / name)
                (directory / 'pairing.json').write_bytes(payload)
                with self.assertRaises(ValueError):
                    Pairing(directory)

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
        b.session.connection = FakeChannel('usb')
        b.session.transport_kind = 'usb'
        return b

    def events(self, channel):
        return [
            decode_event(receive(SimpleNamespace(recv=io.BytesIO(raw).read)).data)
            for raw in channel.sent
        ]

    def hellos(self, channel):
        return [event for event in self.events(channel) if event['event'] == 'hello']

    def test_failed_screen_wake_reaches_replacement_without_restarting_capture(self):
        class WakeWriteFails(FakeChannel):
            def sendall(self, raw):
                event = decode_event(receive(SimpleNamespace(recv=io.BytesIO(raw).read)).data)
                if event['event'] == 'screen_wake':
                    raise BrokenPipeError('fixture lost screen wake')
                super().sendall(raw)

        for all_routes_lost, sleeps_again in ((False, False), (True, False), (False, True)):
            with self.subTest(all_routes_lost=all_routes_lost, sleeps_again=sleeps_again):
                b = self.bridge()
                b.session.active = True
                b.session.screen_sleeping = True
                b.session.connection = WakeWriteFails('usb')
                with self.assertRaises(BrokenPipeError):
                    b.session.touch_activity()
                if all_routes_lost:
                    b.change_transport(None)
                failed_route = WakeWriteFails('wifi')
                b.change_transport(failed_route)
                ready = {'event': 'device_ready', 'capture_active': not all_routes_lost}
                with self.assertRaises(BrokenPipeError):
                    b.on_event(ready)
                if sleeps_again:
                    with patch(
                        'session_controller.time.monotonic',
                        return_value=b.session.last_interaction + 901,
                    ):
                        b.session.idle_tick()
                restored = FakeChannel('usb')
                b.change_transport(restored)
                b.on_event(ready)
                b.on_event(ready)
                self.assertEqual(
                    sum(event['event'] == 'screen_wake' for event in self.events(restored)),
                    int(not sleeps_again),
                )
                self.assertEqual(b.session.screen_sleeping, sleeps_again)
                self.assertEqual(b.session.active, not all_routes_lost)

    def test_interrupted_playback_notice_reaches_new_route_and_keeps_task_complete(self):
        for active in (False, True):
            with self.subTest(active=active):
                b = self.bridge()
                b.session.active = active
                b.session.epoch = 12
                task = b.workspace.task_start(b.generation, '固定任务')
                b.workspace.task_finish(task, 'completed', '已完成的固定结果')
                b.playback.playing = True
                old = b.session.connection
                new = FakeChannel('wifi')
                b.change_transport(new)
                self.assertFalse(b.playback.playing)
                self.assertFalse(self.hellos(old))
                for _ in range(2):
                    b.on_event({'event': 'device_ready', 'capture_active': active})
                notice, duplicate = self.hellos(new)
                self.assertTrue(notice['playback_interrupted_by_transport'])
                self.assertIn('任务页', notice['message'])
                self.assertFalse(duplicate['playback_interrupted_by_transport'])
                self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
                self.assertEqual(b.session.active, active)
                self.assertEqual(b.session.epoch, 12)
                self.assertTrue(b.response.queue.empty())

    def test_handover_publishes_unconfirmed_task_result_without_replaying(self):
        b = self.bridge()
        task = b.workspace.task_start(b.generation, '固定在途工具')
        b.workspace.task_stage(task, 'tool', 'Dot 正在执行任务', 'running')
        for kind in ('wifi', 'usb'):
            with self.subTest(kind=kind):
                channel = FakeChannel(kind)
                b.change_transport(channel)
                b.on_event({'event': 'device_ready', 'capture_active': False})
                snapshot = [e for e in self.events(channel) if e['event'] == 'workspace'][-1]
                self.assertEqual(len(snapshot['tasks']), 1)
                receipt = snapshot['tasks'][0]
                self.assertEqual(receipt['id'], task)
                self.assertEqual(receipt['status'], 'cancelled')
                self.assertEqual(receipt['stage'], '连接已切换，执行结果待核对。请确认后再试。')
                self.assertEqual(receipt['result'], '')
                self.assertTrue(b.response.queue.empty())
                b.workspace.task_finish(task, 'completed', '迟到结果')

    def test_notice_survives_all_routes_lost_and_a_failed_hello(self):
        b = self.bridge()
        b.session.active = True
        b.playback.playing = True
        b.change_transport(None)
        self.assertFalse(b.session.active)

        class Broken(FakeChannel):
            def sendall(self, raw):
                raise BrokenPipeError('fixture route dropped')

        b.change_transport(Broken('wifi'))
        with self.assertRaises(BrokenPipeError):
            b.on_event({'event': 'device_ready', 'capture_active': False})
        restored = FakeChannel('usb')
        b.change_transport(restored)
        b.on_event({'event': 'device_ready', 'capture_active': False})
        self.assertTrue(self.hellos(restored)[0]['playback_interrupted_by_transport'])
        self.assertFalse(b.session.active)
        self.assertFalse(b.playback.playing)

    def test_idle_and_automatic_calibration_switch_do_not_claim_playback_interruption(self):
        for calibration in (False, True):
            with self.subTest(calibration=calibration):
                b = self.bridge()
                if calibration:
                    b.session.active = True
                    b.calibration.begin()
                    b.playback.playing = True
                new = FakeChannel('wifi')
                b.change_transport(new)
                b.on_event({'event': 'device_ready', 'capture_active': calibration})
                self.assertFalse(self.hellos(new)[0]['playback_interrupted_by_transport'])

    def test_existing_capture_epoch_is_preserved_and_old_audio_is_discarded(self):
        b = self.bridge()
        b.session.active = True
        b.session.epoch = 12
        b.playback.playing = True
        old = b.session.generation
        new = FakeChannel('wifi')
        b.change_transport(new)
        self.assertTrue(b.session.active)
        self.assertEqual(b.session.epoch, 12)
        self.assertGreater(b.session.generation, old)
        self.assertEqual(b.capture.capture_lookahead_ns, 120_000_000)
        b.send(Packet(SPEAKER, old, data=b'\0\0'))
        self.assertFalse(new.sent)
        b.event('caption_timing', generation=old, timings=[])
        self.assertFalse(new.sent)
        b.on_packet(Packet(RENDER, old, data=b'\0\0'))
        self.assertEqual(b.metrics['render_frames'], 0)

    def test_queued_playback_event_cannot_cross_cancellation_or_route_switch(self):
        for switch in (False, True):
            with self.subTest(switch=switch):
                b = self.bridge()
                previous = b.session.connection
                old = b.generation
                entered, release = threading.Event(), threading.Event()
                packet_event = Packet.event

                def encode(event, *args, **fields):
                    packet = packet_event(event, *args, **fields)
                    if event == 'play_begin' and fields.get('playback_id') == 1:
                        entered.set()
                        release.wait(2)
                    return packet

                with patch.object(Packet, 'event', side_effect=encode):
                    worker = threading.Thread(
                        target=b.event,
                        args=('play_begin',),
                        kwargs={'generation': old, 'playback_id': 1, 'text': '固定旧播报'},
                        daemon=True,
                    )
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        if switch:
                            b.change_transport(FakeChannel('wifi'))
                        else:
                            b.cancel('test_cancel')
                        self.assertGreater(b.generation, old)
                        b.event('play_begin', generation=b.generation, playback_id=2)
                    finally:
                        release.set()
                        worker.join(3)

                self.assertFalse(worker.is_alive())
                current = b.session.connection
                wires = current.sent if current is previous else previous.sent + current.sent
                events = [
                    decode_event(receive(SimpleNamespace(recv=io.BytesIO(raw).read)).data)
                    for raw in wires
                ]
                self.assertEqual(
                    [event['playback_id'] for event in events if event['event'] == 'play_begin'],
                    [2],
                )

    def test_generation_is_rechecked_after_waiting_for_the_write_lock(self):
        b = self.bridge()
        generation = b.generation
        queued, cancelled = threading.Event(), threading.Event()
        lock = b.session.write_lock
        event = threading.Thread(
            target=b.event,
            args=('caption_timing',),
            kwargs={'generation': generation, 'playback_id': 1, 'timings': []},
            daemon=True,
        )
        cancel = threading.Thread(target=b.cancel, args=('test_cancel',), daemon=True)

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is event:
                    queued.set()
                lock.acquire()

            def __exit__(self, *unused):
                lock.release()

        b.session.write_lock = ObservedLock()
        with patch.object(b.playback, 'notify', side_effect=cancelled.set):
            with b.session.write_lock:
                event.start()
                self.assertTrue(queued.wait(1))
                cancel.start()
                self.assertTrue(cancelled.wait(1))
            event.join(3)
            cancel.join(3)
        self.assertFalse(event.is_alive())
        self.assertFalse(cancel.is_alive())
        sent = [
            decode_event(receive(SimpleNamespace(recv=io.BytesIO(raw).read)).data)['event']
            for raw in b.session.connection.sent
        ]
        self.assertEqual(sent, ['cancel'])

    def test_losing_all_routes_stops_capture_and_reconnect_does_not_enable_it(self):
        b = self.bridge()
        b.session.active = True
        closed = []

        class Aec:
            def close(self):
                closed.append(True)

        b.capture.echo.aec = Aec()
        b.change_transport(None)
        self.assertFalse(b.session.active)
        self.assertTrue(closed)
        b.change_transport(FakeChannel('wifi'))
        self.assertFalse(b.session.active)
        b.on_event(
            {'event': 'device_ready', 'capture_active': False, 'supports_playback_pause': True}
        )
        self.assertFalse(b.session.active)

    def test_rebind_of_same_user_enabled_epoch_does_not_start_new_capture(self):
        b = self.bridge()
        b.session.active = True
        b.session.epoch = 2
        old = b.session.generation
        b.on_event({'event': 'session_rebind', 'capture_active': True, 'epoch': 2})
        self.assertTrue(b.session.active)
        self.assertEqual(b.session.generation, old)

    def test_route_handover_keeps_the_current_local_wake_prompt(self):
        from wake_controller import WAITING

        for wake_mode, awake in ((True, False), (False, False), (True, True), (False, True)):
            with self.subTest(wake_mode=wake_mode, awake=awake):
                b = self.bridge()
                b.session.active = True
                b.session.epoch = 12
                b.session.wake = wake_mode
                b.session.awake = awake
                new = FakeChannel('wifi')
                b.change_transport(new)
                b.on_event(
                    {'event': 'device_ready', 'capture_active': True, 'wake_mode': wake_mode}
                )
                b.on_event({'event': 'session_rebind', 'capture_active': True, 'epoch': 12})
                hello = self.hellos(new)[-1]
                status = [e for e in self.events(new) if e['event'] == 'status'][-1]
                self.assertEqual(
                    (hello['message'], status['message']),
                    ('我在听', '连接已切换，我在听') if awake else (WAITING, WAITING),
                )
                self.assertTrue(b.session.active)
                self.assertEqual(b.session.awake, awake)
                self.assertEqual(b.session.epoch, 12)


class RebindIdempotenceTests(unittest.TestCase):
    def test_start_and_rebind_for_same_epoch_cannot_duplicate_calibration(self):
        for order in [('session_rebind', 'session_start'), ('session_start', 'session_rebind')]:
            with self.subTest(order=order):
                b = Bridge(None, None)
                b.session.connection = FakeChannel('usb')
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
                    generation = b.session.generation
                    b.on_event({'event': 'session_start', 'epoch': 7})
                    self.assertEqual(b.session.generation, generation)
                    self.assertEqual(calibrate.call_count, 1)
                    b.on_event({'event': 'session_start', 'epoch': 8})
                    deadline = time.monotonic() + 1
                    while calibrate.call_count < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(calibrate.call_count, 2)
                    self.assertEqual(b.session.epoch, 8)

    def test_stopped_capture_is_not_recreated_by_late_rebind(self):
        for stop in ('manual', 'all_routes_lost', 'peer_idle', 'stop_before_start'):
            with self.subTest(stop=stop):
                b = Bridge(None, None)
                b.session.connection = FakeChannel('usb')
                b.save_status = lambda: None
                calibrated = threading.Event()
                with (
                    patch('bridge.WebRtcEchoCanceller'),
                    patch.object(b, 'calibrate', side_effect=lambda _: calibrated.set()),
                ):
                    if stop != 'stop_before_start':
                        b.on_event({'event': 'session_start', 'epoch': 7})
                        self.assertTrue(calibrated.wait(1))
                    if stop == 'all_routes_lost':
                        b.change_transport(None)
                    elif stop == 'peer_idle':
                        b.on_event({'event': 'device_ready', 'capture_active': False})
                    else:
                        b.on_event({'event': 'session_stop', 'epoch': 7})
                    b.change_transport(FakeChannel('wifi'))
                    b.on_event({'event': 'device_ready', 'capture_active': True})
                    stopped_generation = b.generation
                    calibrated.clear()
                    for _ in range(2):
                        b.on_event({'event': 'session_rebind', 'capture_active': True, 'epoch': 7})
                    b.on_packet(Packet(MIC, 7, data=b'\0\0'))
                    self.assertFalse(b.session.active)
                    self.assertEqual(b.generation, stopped_generation)
                    self.assertFalse(calibrated.is_set())
                    self.assertIsNone(b.capture.echo.aec)
                    self.assertEqual(b.metrics['mic_frames'], 0)

                    b.on_event({'event': 'session_start', 'epoch': 9})
                    self.assertTrue(calibrated.wait(1))
                    self.assertTrue(b.session.active)
                    self.assertEqual(b.session.epoch, 9)

    def test_duplicate_rebind_during_calibration_starts_only_one_calibration(self):
        b = Bridge(None, None)
        b.session.connection = FakeChannel('wifi')
        b.session.active = True
        b.session.epoch = 3
        b.calibration.active = True
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
        java = java_bin()
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

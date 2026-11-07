"""Real host output and role negotiation under route changes; no device/models."""

import json
import queue
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bridge import Bridge
from app_runtime import reconcile_transport
from protocol import Packet, EVENT, MIC, SPEAKER, ROUTE_BOUND, decode_event, receive
from route_output import bind_host_packet
from test_barge_in import Sink
from transport import TransportHub
from transport_channel import Channel


class HostRouteOutputTests(unittest.TestCase):
    def connection(self, token='a' * 32, duplex=True):
        c = Sink()
        c.kind = 'usb'
        c.route_token = token
        c.route_duplex = duplex
        return c

    def bridge(self, *, duplex=True, token='a' * 32):
        with patch('threading.Thread.start'):
            b = Bridge(None, None)
        b.session.connection = self.connection(token, duplex)
        b.session.bound_route_token = token
        b.log = lambda *_args, **_fields: None
        b.save_status = lambda: None
        return b

    def test_application_events_and_pcm_are_bound_once_with_all_fields_preserved(self):
        b = self.bridge()
        b.event('caption', text='公开测试')
        caption = b.session.connection.packets[-1]
        self.assertEqual(caption.kind, ROUTE_BOUND)
        self.assertEqual(decode_event(caption.unbind_route('a' * 32).data)['text'], '公开测试')
        pcm = Packet(SPEAKER, b.generation, 12, 320, 1_000_000_000, b'\x01\0')
        b.send(pcm)
        self.assertEqual(b.session.connection.packets[-1].unbind_route('a' * 32), pcm)
        b.send(pcm.bind_route('a' * 32))
        self.assertEqual(b.session.connection.packets[-1].unbind_route('a' * 32), pcm)

    def test_legacy_and_one_way_negotiation_keep_unwrapped_host_output(self):
        for token in (None, 'a' * 32):
            with self.subTest(token=token):
                b = self.bridge(duplex=False, token=token)
                b.event('caption', text='公开测试')
                self.assertEqual(b.session.connection.packets[-1].kind, EVENT)

    def test_old_envelope_is_never_retagged_or_sent_to_a_new_activation(self):
        b = self.bridge()
        p = Packet.event('caption', text='旧内容').bind_route('b' * 32)
        self.assertIs(bind_host_packet(p, (b.session.connection, 'a' * 32)), p)
        b.send(p)
        self.assertEqual(b.session.connection.packets, [])
        b.send(Packet(SPEAKER, b.generation - 1, data=b'\0\0'))
        self.assertEqual(b.session.connection.packets, [])
        b.send(Packet(SPEAKER, b.generation - 1, data=bytes(6402)))
        self.assertEqual(b.session.connection.packets, [])

    def test_screen_waits_for_session_binding_after_same_socket_activation(self):
        b = self.bridge()
        connection = b.session.connection
        connection.route_token = 'b' * 32
        connection.application_ready = True
        b.session.pending_screen_event = 'screen_wake'
        b.session.flush_screen_event()
        self.assertEqual(connection.packets, [])
        self.assertEqual(b.session.pending_screen_event, 'screen_wake')
        b.session.bound_route_token = 'b' * 32
        b.session.flush_screen_event()
        self.assertIsNone(b.session.pending_screen_event)
        self.assertEqual(
            decode_event(connection.packets[-1].unbind_route('b' * 32).data)['event'], 'screen_wake'
        )

    def test_hello_reconciles_desired_screen_even_after_old_route_cleared_intent(self):
        for asleep in (False, True):
            with self.subTest(asleep=asleep):
                b = self.bridge()
                b.session.active = True
                b.session.screen_sleeping = asleep
                b.session.connection.application_ready = True
                event = 'screen_sleep' if asleep else 'screen_wake'
                for _ in range(2):
                    b.session.ready({'capture_active': True})
                    screens = [
                        decode_event(p.unbind_route('a' * 32).data)['event']
                        for p in b.session.connection.packets
                    ]
                    self.assertEqual(screens.count(event), 1)
                    self.assertIsNone(b.session.pending_screen_event)
                connection = b.session.connection
                connection.route_token = 'b' * 32
                b.change_transport(connection)
                b.session.ready({'capture_active': True})
                screens = [
                    decode_event(p.unbind_route(p.data[:16].hex()).data)['event']
                    for p in connection.packets
                ]
                self.assertEqual(screens.count(event), 2)
                self.assertIsNone(b.session.pending_screen_event)
                self.assertTrue(b.session.active)

    def test_sleep_is_logged_only_after_current_route_publication(self):
        b = self.bridge()
        b.session.pending_screen_event = 'screen_sleep'
        connection = b.session.connection
        connection.application_ready = True
        with patch.object(b, 'log') as log:
            with patch.object(
                b,
                'event',
                side_effect=lambda *_args, **_kw: setattr(connection, 'application_ready', False),
            ):
                b.session.flush_screen_event()
            log.assert_not_called()
            self.assertEqual(b.session.pending_screen_event, 'screen_sleep')
            connection.application_ready = True
            b.session.flush_screen_event()
            log.assert_called_once_with('screen_idle_sleep')

    def test_uncertain_role_drops_microphone_but_still_accepts_explicit_stop(self):
        b = self.bridge()
        b.session.active = True
        b.session.epoch = 7
        b.session.connection.application_ready = False
        with patch.object(b.capture, 'on_packet') as capture:
            b.on_packet(Packet(MIC, 7, data=bytes(320)).bind_route('a' * 32))
        capture.assert_not_called()
        b.on_packet(Packet.event('session_stop', epoch=7).bind_route('a' * 32))
        self.assertFalse(b.session.active)
        self.assertTrue(b.session.capture_start_required)
        b.session.connection.application_ready = True
        b.on_packet(
            Packet.event('session_rebind', epoch=7, capture_active=True).bind_route('a' * 32)
        )
        self.assertFalse(b.session.active)

    def test_screen_intent_is_retained_until_its_current_route_has_published_it(self):
        for reused in (False, True):
            with self.subTest(reused=reused):
                b = self.bridge()
                old = b.session.connection
                old.application_ready = False
                b.session.pending_screen_event = 'screen_wake'
                b.session.flush_screen_event()
                self.assertEqual(old.packets, [])
                self.assertEqual(b.session.pending_screen_event, 'screen_wake')
                old.application_ready = True

                def handover_during_send(*_args, **_fields):
                    if reused:
                        old.application_ready = False
                    else:
                        b.session.connection = self.connection('b' * 32)
                        b.session.bound_route_token = 'b' * 32

                with patch.object(b, 'event', side_effect=handover_during_send):
                    b.session.flush_screen_event()
                self.assertEqual(b.session.pending_screen_event, 'screen_wake')
                b.session.connection.application_ready = True
                b.session.flush_screen_event()
                self.assertIsNone(b.session.pending_screen_event)
                token = b.session.bound_route_token
                sent = b.session.connection.packets[-1].unbind_route(token)
                self.assertEqual(decode_event(sent.data)['event'], 'screen_wake')

    def test_waiting_generationless_event_cannot_move_to_new_or_reused_connection(self):
        for reused in (False, True):
            with self.subTest(reused=reused):
                b = self.bridge()
                old = b.session.connection
                prepared = threading.Event()
                errors = []
                real_bind = bind_host_packet

                def observe(packet, owner, generation=None):
                    result = real_bind(packet, owner, generation)
                    if packet.kind == EVENT and decode_event(packet.data)['event'] == 'public_test':
                        prepared.set()
                    return result

                def send():
                    try:
                        b.event('public_test', text='不得迁移的旧结果')
                    except BaseException as error:
                        errors.append(error)

                with patch('session_controller.bind_host_packet', side_effect=observe):
                    with b.session.write_lock:
                        worker = threading.Thread(target=send)
                        worker.start()
                        self.assertTrue(prepared.wait(2))
                        next_connection = old if reused else self.connection('b' * 32)
                        next_connection.route_token = 'b' * 32
                        b.change_transport(next_connection)
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                for connection in (old, b.session.connection):
                    for packet in connection.packets:
                        inner = (
                            packet.unbind_route(packet.data[:16].hex())
                            if packet.kind == ROUTE_BOUND
                            else packet
                        )
                        if inner.kind == EVENT:
                            self.assertNotEqual(decode_event(inner.data)['event'], 'public_test')
                b.event('public_test', text='当前结果')
                current = b.session.connection.packets[-1].unbind_route('b' * 32)
                self.assertEqual(decode_event(current.data)['text'], '当前结果')


class HostRouteNegotiationTests(unittest.TestCase):
    def peer(self, kind):
        a, peer = socket.socketpair()
        self.addCleanup(peer.close)
        channel = Channel(a, kind, queue.Queue())
        self.addCleanup(channel.close)
        channel.ready = channel.route_envelope = channel.route_duplex = True
        return channel, peer

    def test_role_controls_are_unwrapped_ordered_and_lan_config_is_bound(self):
        hub = TransportHub(
            SimpleNamespace(device_config=lambda: {'public': 'test'}), lambda: None, lambda: True
        )
        wifi, wp = self.peer('wifi')
        hub.channels['wifi'] = wifi
        self.assertTrue(hub.selected(wifi))
        first = receive(wp)
        usb, up = self.peer('usb')
        hub.channels['usb'] = usb
        self.assertTrue(hub.selected(usb))
        self.assertEqual(decode_event(receive(wp).data)['event'], 'transport_standby')
        second = receive(up)
        config = receive(up)
        self.assertEqual(config.kind, ROUTE_BOUND)
        self.assertEqual(
            decode_event(config.unbind_route(usb.route_token).data)['event'], 'lan_config'
        )
        self.assertTrue(hub.selected(wifi))
        self.assertEqual(decode_event(receive(up).data)['event'], 'transport_standby')
        third = receive(wp)
        rows = [decode_event(p.data) for p in (first, second, third)]
        self.assertTrue(all(p.kind == EVENT for p in (first, second, third)))
        self.assertEqual([r['route_serial'] for r in rows], [1, 2, 3])
        self.assertEqual(len({r['route_owner'] for r in rows}), 1)
        self.assertEqual(len({r['route_token'] for r in rows}), 3)
        self.assertTrue(all(r['route_output_version'] == 1 for r in rows))
        summary = json.dumps(hub.diagnostics())
        self.assertNotIn(rows[0]['route_owner'], summary)
        self.assertNotIn(rows[-1]['route_token'], summary)
        self.assertEqual(hub.diagnostics()['route_output_version'], 1)

    def test_failed_activation_still_consumes_its_serial(self):
        class Failed:
            def sendall(self, raw):
                raise OSError('controlled failure')

            def shutdown(self, how):
                pass

            def close(self):
                pass

        hub = TransportHub(None, lambda: None, lambda: False)
        failed = Channel(Failed(), 'wifi', queue.Queue())
        failed.ready = failed.route_envelope = failed.route_duplex = True
        self.addCleanup(failed.close)
        hub.channels['wifi'] = failed
        self.assertFalse(hub.selected(failed))
        good, peer = self.peer('wifi')
        hub.channels['wifi'] = good
        self.assertTrue(hub.selected(good))
        self.assertEqual(decode_event(receive(peer).data)['route_serial'], 2)

    def test_route_closed_after_write_cannot_publish_application_readiness(self):
        hub = TransportHub(None, lambda: None, lambda: False)
        channel, peer = self.peer('wifi')
        hub.channels['wifi'] = channel
        original = channel.event

        def send_then_close(*args, **fields):
            original(*args, **fields)
            channel.close()

        with patch.object(channel, 'event', side_effect=send_then_close):
            self.assertFalse(hub.selected(channel))
        self.assertTrue(hub.role_uncertain)
        self.assertFalse(channel.application_ready)
        self.assertIsNone(hub.active)
        self.assertEqual(hub.diagnostics()['route_output_version'], 0)

    def test_failed_selection_reactivates_surviving_route_with_a_fresh_serial(self):
        class Failed:
            def sendall(self, raw):
                raise OSError('controlled failure')

            def shutdown(self, how):
                pass

            def close(self):
                pass

        hub = TransportHub(None, lambda: None, lambda: False)
        wifi, peer = self.peer('wifi')
        hub.channels['wifi'] = wifi
        self.assertTrue(hub.selected(wifi))
        first = decode_event(receive(peer).data)
        b = HostRouteOutputTests().bridge()
        b.session.connection = wifi
        b.session.bound_route_token = wifi.route_token
        failed = Channel(Failed(), 'usb', queue.Queue())
        failed.ready = failed.route_envelope = failed.route_duplex = True
        self.addCleanup(failed.close)
        hub.channels['usb'] = failed
        self.assertFalse(hub.selected(failed))
        self.assertEqual(decode_event(receive(peer).data)['event'], 'transport_standby')
        self.assertIs(hub.active, wifi)
        self.assertTrue(hub.role_uncertain)
        self.assertFalse(wifi.application_ready)
        b.session.pending_screen_event = 'screen_wake'
        b.session.flush_screen_event()
        self.assertEqual(b.session.pending_screen_event, 'screen_wake')
        self.assertTrue(reconcile_transport(b, hub))
        activated = decode_event(receive(peer).data)
        self.assertEqual(activated['event'], 'transport_activate')
        self.assertEqual(activated['route_serial'], 3)
        self.assertNotEqual(first['route_token'], activated['route_token'])
        self.assertFalse(hub.role_uncertain)
        self.assertTrue(wifi.application_ready)
        self.assertFalse(b.session.active)
        self.assertEqual(b.session.pending_screen_event, 'screen_wake')
        b.session.flush_screen_event()
        self.assertIsNone(b.session.pending_screen_event)

    def test_duplex_requires_both_exact_advertised_capabilities_before_ready(self):
        for envelope, output in ((1, 1), (1, True), (1, '1'), (1, None), (True, 1), (0, 1)):
            with self.subTest(envelope=envelope, output=output):
                a, peer = socket.socketpair()
                self.addCleanup(peer.close)
                events = queue.Queue()
                expected = (
                    type(envelope) is int and envelope == 1 and type(output) is int and output == 1
                )

                class Published(Channel):
                    def __setattr__(self, name, value):
                        if name == 'ready' and value is True and self.route_duplex != expected:
                            raise AssertionError('incomplete capability published')
                        super().__setattr__(name, value)

                channel = Published(a, 'wifi', events)
                self.addCleanup(channel.close)
                channel.start()
                fields = {'route_envelope_version': envelope}
                if output is not None:
                    fields['route_output_version'] = output
                peer.sendall(Packet.event('transport_hello', protocol=2, **fields).encode())
                self.assertEqual(events.get(timeout=2)[:2], ('up', channel))
                self.assertEqual(channel.route_duplex, expected)
                channel.close()
                channel.reader.join(2)
                self.assertFalse(channel.reader.is_alive())

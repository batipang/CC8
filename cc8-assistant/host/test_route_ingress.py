"""Fresh activation envelopes fence queued controls/PCM without device capture."""

import json
import queue
import socket
import unittest
from unittest.mock import patch

import numpy as np
from hypothesis import given, seed, settings, strategies as st

import test_fast_stop_route as fixtures
from dsp import FRAME
from echo_pipeline import CleanFrame
from protocol import Packet, MIC, MIC_PAIRED, EVENT, ROUTE_BOUND, receive
from test_protocol_fuzz import Fragmented
from transport import TransportHub
from transport_channel import Channel


class RouteIngressTests(unittest.TestCase):
    def bridge(self):
        b = fixtures.FastStopRouteTests().bridge('usb')
        b.session.epoch = 7
        b.session.capture_id = 'a' * 32
        b.session.capture_start_ack = True
        b.capture.echo.process = lambda stamp, raw: CleanFrame(
            stamp, raw, np.zeros(FRAME), np.zeros(FRAME), raw
        )
        return b

    def select(self, b, route=None):
        route = route or fixtures.Channel('wifi')
        route.route_token = 'c' * 32
        b.change_transport(route)
        b.on_packet(
            Packet.event(
                'device_ready',
                capture_active=True,
                capture_reject_version=1,
                capture_start_ack_version=1,
            ).bind_route(route.route_token)
        )
        b.playback.playing = True
        return route

    def bind(self, b, token=None, **fields):
        p = Packet.event(
            'session_rebind',
            capture_active=True,
            epoch=7,
            capture_id='a' * 32,
            capture_start_token=b.session.capture_start_token,
            **fields,
        )
        b.on_packet(p.bind_route(token or b.session.connection.route_token))

    def audio(self, b, token=None, *, start=0):
        for i in range(16):
            value = 4096 if i == 0 else 8192 if i == 1 else 0
            p = Packet(
                MIC,
                7,
                start + i,
                (start + i) * FRAME,
                3_000_000_000 + (start + i) * 10_000_000,
                np.full(FRAME, value, dtype='<i2').tobytes(),
            )
            b.on_packet(p if token is None else p.bind_route(token))

    def test_preserved_capture_drops_whole_stop_command_before_current_rebind(self):
        b = self.bridge()
        self.select(b)
        generation = b.session.generation
        self.audio(b, 'c' * 32)
        self.assertFalse(b.session.rebind_seen)
        self.assertTrue(b.playback.playing)
        self.assertEqual(b.session.generation, generation)
        self.assertEqual(b.metrics['mic_frames'], 0)
        self.assertEqual(len(b.capture.pending_mic), 0)
        self.bind(b)
        self.assertTrue(b.session.rebind_seen)
        self.audio(b, 'c' * 32, start=20)
        self.assertEqual(b.metrics['fast_stop_count'], 1)
        self.assertFalse(b.playback.playing)

    def test_old_control_cannot_confirm_new_route_or_stop_preserved_capture(self):
        for name in ('device_ready', 'session_rebind', 'session_start', 'session_stop'):
            with self.subTest(event=name):
                b = self.bridge()
                self.select(b)
                generation = b.session.generation
                p = Packet.event(
                    name,
                    capture_active=name != 'device_ready',
                    epoch=7,
                    capture_id='a' * 32,
                    capture_start_token=b.session.capture_start_token,
                )
                b.on_packet(p.bind_route('b' * 32))
                self.assertFalse(b.session.rebind_seen)
                self.assertTrue(b.session.active)
                self.assertEqual(b.session.generation, generation)
                self.audio(b, 'c' * 32)
                self.assertEqual(b.metrics['mic_frames'], 0)

    def test_reused_usb_object_rejects_old_pcm_even_after_fresh_rebind(self):
        b = self.bridge()
        usb = b.session.connection
        usb.route_token = 'b' * 32
        self.select(b)
        usb.route_token = 'd' * 32
        b.change_transport(usb)
        b.playback.playing = True
        b.session.capture_start_ack = True
        self.bind(b, 'd' * 32)
        self.assertTrue(b.session.rebind_seen)
        generation = b.session.generation
        self.audio(b, 'b' * 32)
        self.audio(b)  # Negotiated routes never accept legacy/unwrapped packets.
        self.assertEqual(b.metrics['mic_frames'], 0)
        self.assertEqual(b.session.generation, generation)
        self.audio(b, 'd' * 32, start=20)
        self.assertEqual(b.metrics['fast_stop_count'], 1)

    def test_new_activation_on_same_channel_reopens_the_registration_gate(self):
        b = self.bridge()
        route = self.select(b)
        self.bind(b)
        self.assertFalse(b.session.capture_route_pending)
        route.route_token = 'd' * 32
        b.change_transport(route)
        self.assertTrue(b.session.capture_route_pending)
        self.assertFalse(b.session.rebind_seen)
        self.bind(b, 'c' * 32)
        self.assertFalse(b.session.rebind_seen)
        b.session.capture_start_ack = True
        self.bind(b, 'd' * 32)
        self.assertFalse(b.session.capture_route_pending)

    def test_late_rebind_after_stop_does_not_restart_or_admit_audio(self):
        b = self.bridge()
        self.select(b)
        b.session.capture_start_ack = True
        self.bind(b)
        b.stop()
        self.bind(b, capture_frontend='mono')
        self.audio(b, 'c' * 32)
        self.assertFalse(b.session.active)
        self.assertEqual(b.metrics['mic_frames'], 0)

    def test_legacy_route_also_drops_pcm_until_rebind(self):
        b = self.bridge()
        b.change_transport(fixtures.Channel('wifi'))
        self.audio(b)
        self.assertEqual(b.metrics['mic_frames'], 0)
        self.assertEqual(len(b.capture.pending_mic), 0)

    def test_valid_ready_is_bound_without_exposing_token_to_application_events(self):
        b = self.bridge()
        self.select(b)
        with patch.object(b.device_events.session, 'ready') as ready:
            b.on_packet(Packet.event('device_ready', capture_active=True).bind_route('c' * 32))
        ready.assert_called_once_with({'event': 'device_ready', 'capture_active': True})


class RouteFramingTests(unittest.TestCase):
    @seed(20261107)
    @settings(max_examples=100, deadline=500, database=None)
    @given(
        kind=st.sampled_from((MIC, MIC_PAIRED)),
        raw=st.binary(min_size=1, max_size=1600),
        chunk=st.integers(1, 100),
    )
    def test_bound_pcm_roundtrip_preserves_all_header_fields_and_channels(self, kind, raw, chunk):
        p = Packet(kind, 7, 18, 320, 1_234_567_890, raw * 4)
        bound = p.bind_route('a' * 32)
        self.assertEqual(receive(Fragmented(bound.encode(), chunk)).unbind_route('a' * 32), p)
        self.assertIsNone(bound.unbind_route('b' * 32))

    def test_events_size_and_nested_envelopes_remain_bounded(self):
        p = Packet.event('session_rebind', epoch=7, capture_active=True)
        self.assertEqual(
            receive(Fragmented(p.bind_route('a' * 32).encode())).unbind_route('a' * 32), p
        )
        for data in (
            b'',
            bytes(16),
            bytes(16) + bytes([ROUTE_BOUND]),
            bytes(16) + bytes([MIC]) + b'x',
        ):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                Packet(ROUTE_BOUND, data=data).encode()
        with self.assertRaises(ValueError):
            p.bind_route('a' * 32).bind_route('a' * 32)
        with self.assertRaises(ValueError):
            Packet(EVENT, data=bytes(65536 - 29)).bind_route('a' * 32)
        for bad in ('', 'A' * 32, 'a' * 31, False):
            with self.subTest(token=bad), self.assertRaises(ValueError):
                p.bind_route(bad)

    def test_capability_is_negotiated_by_the_actual_channel_reader(self):
        a, peer = socket.socketpair()
        self.addCleanup(peer.close)
        events = queue.Queue()

        class PublishedChannel(Channel):
            def __setattr__(self, name, value):
                if name == 'ready' and value is True and not self.route_envelope:
                    raise AssertionError('Ready published before capability negotiation')
                super().__setattr__(name, value)

        channel = PublishedChannel(a, 'wifi', events)
        self.addCleanup(channel.close)
        channel.start()
        peer.sendall(Packet.event('transport_hello', protocol=2, route_envelope_version=1).encode())
        self.assertEqual(events.get(timeout=2)[:2], ('up', channel))
        self.assertTrue(channel.route_envelope)
        hub = TransportHub(None, lambda: None, lambda: False)
        hub.channels['wifi'] = channel
        self.assertTrue(hub.selected(channel))
        activation = json.loads(receive(peer).data)
        token = activation['route_token']
        ready = Packet.event('device_ready', capture_active=False).bind_route(token)
        peer.sendall(ready.encode())
        kind, source, delivered = events.get(timeout=2)
        self.assertEqual((kind, source), ('packet', channel))
        self.assertEqual(
            delivered.unbind_route(token), Packet.event('device_ready', capture_active=False)
        )
        channel.close()
        channel.reader.join(2)
        self.assertFalse(channel.reader.is_alive())

    def test_every_real_channel_activation_rotates_token_including_reused_socket(self):
        a, peer = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(peer.close)
        channel = Channel(a, 'wifi', queue.Queue())
        channel.ready = channel.route_envelope = True
        hub = TransportHub(None, lambda: None, lambda: False)
        hub.channels['wifi'] = channel
        tokens = []
        for _ in range(3):
            self.assertTrue(hub.selected(channel))
            event = json.loads(receive(peer).data)
            self.assertEqual(event['route_token'], channel.route_token)
            tokens.append(event['route_token'])
        self.assertEqual(len(set(tokens)), 3)
        self.assertNotIn(tokens[0], json.dumps(hub.diagnostics()))
        channel.close()

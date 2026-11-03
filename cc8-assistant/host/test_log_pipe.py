"""A lost supervisor log reader must not break the live connection pipeline."""

from contextlib import contextmanager, redirect_stdout
import io
import json
import os
import queue
import socket
from types import SimpleNamespace
import unittest

from bridge import Bridge
from protocol import receive
from status_reporter import StatusReporter
from transport import Channel, TransportHub


@contextmanager
def broken_pipe_stdout():
    reader, writer = os.pipe()
    os.close(reader)
    # No buffering: closing the test sink cannot create a second flush error.
    with io.TextIOWrapper(io.FileIO(writer, 'w'), write_through=True) as output:
        with redirect_stdout(output):
            yield


class LogPipeTests(unittest.TestCase):
    def reporter(self):
        # log() has no model/resource dependencies; use the production method.
        return StatusReporter.__new__(StatusReporter)

    def test_real_pipe_without_reader_does_not_escape_into_worker(self):
        with broken_pipe_stdout():
            try:
                for _ in range(3):
                    self.reporter().log('fixed_public_test_event', count=1)
            except OSError as error:
                self.fail(f'A detached log reader must not fail a worker: {type(error).__name__}')

    def test_closed_text_output_does_not_fail_worker(self):
        output = io.StringIO()
        output.close()
        with redirect_stdout(output):
            try:
                self.reporter().log('fixed_public_test_event')
            except ValueError:
                self.fail('A closed diagnostic output must not fail a worker')

    def test_healthy_output_keeps_json_fields_and_single_line(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.reporter().log('fixed_public_test_event', count=2)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event['event'], 'fixed_public_test_event')
        self.assertEqual(event['count'], 2)
        self.assertIsInstance(event['monotonic_seconds'], (int, float))

    def test_serialization_errors_remain_visible_to_the_caller(self):
        with broken_pipe_stdout(), self.assertRaises(TypeError):
            self.reporter().log('fixed_public_test_event', invalid=object())

    def test_transport_activation_handshake_and_heartbeat_survive_broken_log_pipe(self):
        bridge = Bridge(None, None)
        bridge.save_status = lambda: None
        peer, stream = socket.socketpair()
        self.addCleanup(peer.close)
        self.addCleanup(stream.close)
        peer.settimeout(2)
        channel = Channel(stream, 'usb', queue.Queue())
        channel.ready = True
        hub = TransportHub(
            SimpleNamespace(device_config=lambda: {}),
            lambda: None,
            lambda: False,
            log=bridge.log,
        )
        hub.channels['usb'] = channel
        self.addCleanup(hub.close)
        with broken_pipe_stdout():
            try:
                bridge.change_transport(channel)
                hub.selected(channel)
                bridge.on_event({'event': 'device_ready', 'capture_active': False})
                bridge.heartbeat_tick()
            except OSError as error:
                self.fail(f'Diagnostic output broke the connection: {type(error).__name__}')
        events = []
        for _ in range(16):
            events.append(json.loads(receive(peer).data)['event'])
            if events[-1] == 'ping':
                break
        self.assertEqual(
            events[:5],
            ['transport_activate', 'lan_config', 'hello', 'brain_provider', 'workspace'],
        )
        self.assertEqual(events[-1], 'ping')
        self.assertIs(hub.active, channel)
        self.assertTrue(bridge.health()['connected'])
        self.assertFalse(bridge.health()['microphone_active'])
        self.assertFalse(channel.closed)


if __name__ == '__main__':
    unittest.main()

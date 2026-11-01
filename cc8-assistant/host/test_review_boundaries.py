"""Regression cases from the full review; no cloud requests or device capture."""

import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from bridge import Bridge
from dot_client import DotBrain, Turn, answer_sources
import launch
import minimax_client as mini
from protocol import MIC, EVENT, Packet
from test_dot_client import FakeRpc
from test_minimax import Session
from transport import TransportHub
from configuration import load_config
from web_search import public_url


class StreamBoundsTests(unittest.TestCase):
    def test_split_utf8_and_crlf_stream_keeps_text(self):
        raw = 'data: {"text":"你好"}\r\n\r\ndata: [DONE]\n\n'.encode()
        events = list(mini.sse_events(mini.bounded_lines(bytes([v]) for v in raw)))
        self.assertEqual(events, [{'text': '你好'}])

    def test_missing_newline_and_aggregate_event_have_limits(self):
        for chunks, options in [
            ([b'x' * 9], {'max_line': 8}),
            ([b'1234\n'] * 3, {'max_total': 10}),
        ]:
            with self.assertRaises(mini.MiniMaxError):
                list(mini.bounded_lines(chunks, **options))
        with patch.object(mini, 'MAX_SSE_EVENT', 12), self.assertRaises(mini.MiniMaxError):
            list(mini.sse_events(['data: 123456', 'data: 123456']))

    def test_cancel_is_checked_even_when_stream_has_only_keepalives(self):
        current = [True]

        def chunks():
            yield b': ping\n'
            current[0] = False
            yield b': ping\n'

        with self.assertRaises(mini.Cancelled):
            list(mini.sse_events(mini.bounded_lines(chunks(), is_current=lambda: current[0])))

    def test_non_object_events_and_bad_utf8_are_rejected(self):
        with self.assertRaises(mini.MiniMaxError):
            list(mini.sse_events(['data: []', '']))

        class Response:
            status_code = 200
            headers = {'Content-Type': 'text/event-stream'}

            def iter_content(self, **kwargs):
                yield b'\xff\n'

            def close(self):
                self.closed = True

        response = Response()
        session = Session(response)
        client = mini.Transport(key='test', session_factory=lambda: session)
        with self.assertRaises(mini.MiniMaxError):
            list(client.events('/v1/t2a_v2', {}))
        self.assertTrue(session.closed)
        self.assertTrue(response.closed)

    def test_non_stream_error_body_is_bounded_without_response_json(self):
        class Response:
            status_code = 200
            headers = {'Content-Type': 'application/json'}

            def iter_content(self, **kwargs):
                for _ in range(3):
                    yield b'x' * (1024 * 1024)

            def json(self):
                raise AssertionError('Unbounded response.json must not run')

            def close(self):
                self.closed = True

        response = Response()
        session = Session(response)
        client = mini.Transport(key='test', session_factory=lambda: session)
        with self.assertRaisesRegex(mini.MiniMaxError, 'size limit'):
            list(client.events('/v1/t2a_v2', {}))
        self.assertTrue(session.closed)
        self.assertTrue(response.closed)

    def test_only_official_https_origin_accepts_credentials(self):
        for base in ['https://api.minimax.cn:8080', 'https://api.minimax.cn/other']:
            with self.assertRaises(ValueError):
                mini.Transport(key='test', base=base)

    def test_credential_file_requires_owner_only_access(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {'CC8_MINIMAX_API_KEY': ''}),
        ):
            defaults = Path(folder) / 'cc8-assistant/host/voice-config.json'
            defaults.parent.mkdir(parents=True)
            defaults.write_text('{"asr":"minimax"}')
            config = load_config(root=Path(folder), environ={})
            credential = Path(folder) / '.toolchains/cc8/voice-secrets.env'
            credential.parent.mkdir(parents=True)
            credential.write_text('CC8_MINIMAX_API_KEY=sk-cp-test-not-a-secret\n')
            credential.chmod(0o644)
            with self.assertRaisesRegex(mini.MiniMaxError, 'owner-only'):
                mini.subscription_key(config=config)
            credential.chmod(0o600)
            self.assertEqual(mini.subscription_key(config=config), 'sk-cp-test-not-a-secret')


class TurnOwnershipTests(unittest.TestCase):
    def test_sources_cannot_point_at_private_or_credential_endpoints(self):
        text = ' '.join(
            '[' + str(i) + '](' + url + ')'
            for i, url in enumerate(
                [
                    'https://127.0.0.1/admin',
                    'https://192.168.1.1/status',
                    'https://device.local/admin',
                    'https://u:p@example.com/path',
                    'https://example.com:8443/',
                    'https://example.com/news',
                ]
            )
        )
        self.assertEqual([row['url'] for row in answer_sources(text)], ['https://example.com/news'])
        for host in ['127.1', '0177.0.0.1', '0x7f.0.0.1']:
            self.assertIsNone(public_url('https://' + host + '/status'))

    def test_old_tool_request_before_new_ack_cannot_execute(self):
        brain = DotBrain(FakeRpc())
        brain.thread_id = 'voice-thread'
        turn = Turn()
        brain.current = turn
        calls = []
        brain.device_control = lambda *args: calls.append(args) or {'ok': True}
        request = {
            'threadId': 'voice-thread',
            'turnId': 'old-turn',
            'tool': 'cc8_control',
            'arguments': {'action': 'volume', 'percent': 20},
        }
        result = []
        started = threading.Event()

        def invoke():
            started.set()
            result.append(brain.call_tool(turn, request))

        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertTrue(started.wait(1))
        turn.ident = 'new-turn'
        turn.ident_ready.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]['error'], 'stale_turn')
        self.assertEqual(calls, [])
        request['turnId'] = 'new-turn'
        self.assertTrue(brain.call_tool(turn, request)['ok'])
        self.assertEqual(calls, [('volume', 20)])
        turn.stopped.set()
        self.assertFalse(brain.call_tool(turn, request)['ok'])
        self.assertEqual(len(calls), 1)

    def test_launcher_never_matches_other_checkout_or_embedded_command_text(self):
        good = f'{os.getuid()} /usr/bin/python3 {launch.BRIDGE}'
        for line, expected in [
            (good, True),
            (f'{os.getuid()} /usr/bin/python3 /other/cc8-assistant/host/bridge.py', False),
            (f'{os.getuid()} /usr/bin/python3 -c "print(\'{launch.BRIDGE}\')"', False),
            (f'{os.getuid() + 1} /usr/bin/python3 {launch.BRIDGE}', False),
            (f'{os.getuid()} /usr/bin/python3 /other.py {launch.BRIDGE}', False),
            ('not-a-uid python bridge.py', False),
        ]:
            with patch.object(
                launch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, line, '')
            ):
                self.assertEqual(launch.own_process(123), expected)


class CaptureAndTransportTests(unittest.TestCase):
    def bridge(self):
        value = Bridge(None, None)
        value.save_status = lambda: None
        value.event = lambda *a, **kw: None
        return value

    def test_late_stop_from_previous_microphone_does_not_close_new_session(self):
        value = self.bridge()
        value.active = True
        value.epoch = 8
        value.on_event({'event': 'session_stop', 'epoch': 6})
        self.assertTrue(value.active)
        value.on_event({'event': 'session_stop', 'epoch': 8})
        self.assertFalse(value.active)

    def test_malformed_device_events_fail_as_protocol_errors(self):
        value = self.bridge()
        for data in [b'[]', b'{"event":"play_done"}', b'{"event":"session_start","epoch":null}']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                value.on_packet(Packet(EVENT, data=data))

    def test_lookahead_rejects_non_monotonic_or_stalled_capture_clock(self):
        value = self.bridge()
        value.active = True
        value.epoch = 1
        value.on_packet(Packet(MIC, 1, nanos=100, data=b'\0' * 320))
        with self.assertRaises(ValueError):
            value.on_packet(Packet(MIC, 1, nanos=99, data=b'\0' * 320))
        value.capture.pending_mic.clear()
        for nanos in range(64):
            value.on_packet(Packet(MIC, 1, nanos=nanos, data=b'\0' * 320))
        with self.assertRaises(ValueError):
            value.on_packet(Packet(MIC, 1, nanos=64, data=b'\0' * 320))

    def test_old_completion_ids_do_not_accumulate_or_finish_current_playback(self):
        value = self.bridge()
        value.playback.playing = True
        for old in range(100):
            value.on_event({'event': 'play_done', 'generation': old})
        self.assertEqual(value.playback.completed, set())
        self.assertTrue(value.playback.playing)
        value.on_event({'event': 'play_done', 'generation': value.generation})
        self.assertEqual(value.playback.completed, {value.generation})
        self.assertFalse(value.playback.playing)

    def test_old_calibration_cannot_replace_new_session_echo_state(self):
        value = self.bridge()
        value.active = True
        value.connection = object()
        value.calibrating = True
        state = object()
        value.capture.echo.aec = state
        value.capture.echo.calibration_frames = [('new',)]
        self.assertFalse(value.align_reference(value.generation - 1))
        self.assertIs(value.capture.echo.aec, state)
        self.assertEqual(value.capture.echo.calibration_frames, [('new',)])
        self.assertTrue(value.calibrating)

    def test_cancel_during_alignment_cannot_finish_the_next_calibration(self):
        value = self.bridge()
        value.active = True
        value.connection = object()
        value.calibrating = True
        generation = value.generation

        def switched(*args):
            value.generation += 1
            return True

        with (
            patch.object(value, 'startup_reference', return_value=object()),
            patch.object(value, 'play', return_value=True),
            patch.object(value, 'align_reference', side_effect=switched),
            patch('bridge.time.sleep'),
        ):
            value.calibrate(generation)
        self.assertTrue(value.calibrating)

    def test_partial_hub_start_failure_closes_already_bound_tcp_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(('127.0.0.1', 0))
            hub = TransportHub(
                None,
                lambda: None,
                lambda: False,
                bind='127.0.0.1',
                tls_port=0,
                discovery_port=occupied.getsockname()[1],
            )
            with self.assertRaises(OSError):
                hub.start()
            self.assertEqual(hub.server.fileno(), -1)
            self.assertEqual(hub.discovery.fileno(), -1)

    def test_shutdown_rejects_a_late_authenticated_connection(self):
        hub = TransportHub(None, lambda: None, lambda: False)
        hub.close()
        first, second = socket.socketpair()
        self.addCleanup(second.close)
        with self.assertRaises(RuntimeError):
            hub.register(first, 'wifi')
        self.assertEqual(first.fileno(), -1)


if __name__ == '__main__':
    unittest.main()

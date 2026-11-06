"""Regression cases from the full review; no cloud requests or device capture."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from dsp import FRAME

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
        bridge = str(launch.BRIDGE)
        for owner, args, expected in (
            (os.getuid(), ['python3', bridge], True),
            (os.getuid(), ['python3', '-u', bridge], True),
            (os.getuid(), ['python3', '/other/cc8-assistant/host/bridge.py'], False),
            (os.getuid(), ['python3', '-c', f'print({bridge!r})'], False),
            (os.getuid() + 1, ['python3', bridge], False),
            (os.getuid(), ['python3', '/other.py', bridge], False),
            ('invalid', ['python3', bridge], False),
            (os.getuid(), [], False),
        ):
            with (
                patch.object(
                    launch.subprocess,
                    'run',
                    return_value=subprocess.CompletedProcess([], 0, str(owner), ''),
                ),
                patch.object(launch, 'process_arguments', return_value=args),
            ):
                self.assertEqual(launch.own_process(123), expected)

    def test_launcher_rechecks_transiently_unavailable_arguments(self):
        with (
            patch.object(
                launch.subprocess, 'run', return_value=SimpleNamespace(stdout=str(os.getuid()))
            ),
            patch.object(
                launch, 'process_arguments', side_effect=([], ['python', '-u', str(launch.BRIDGE)])
            ) as args,
            patch.object(launch.time, 'sleep'),
        ):
            self.assertTrue(launch.own_process(123))
        self.assertEqual(args.call_count, 2)

    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'kernel argv reader platform')
    def test_launcher_owns_spaced_worker_and_rejects_flattened_argument_collision(self):
        with tempfile.TemporaryDirectory(prefix='cc8 checkout ') as directory:
            root = Path(directory) / 'project space'
            bridge = root / 'cc8-assistant/host/bridge.py'
            bridge.parent.mkdir(parents=True)
            decoy = root.with_name('project')
            for script in (bridge, decoy):
                script.write_text('import time\ntime.sleep(10)\n')
            commands = ([str(bridge)], [str(decoy), 'space/cc8-assistant/host/bridge.py'])
            for args, owned in zip(commands, (True, False), strict=True):
                with (
                    subprocess.Popen([sys.executable, '-u', *args]) as child,
                    patch.object(launch, 'BRIDGE', bridge),
                ):
                    try:
                        self.assertIs(launch.own_process(child.pid), owned)
                    finally:
                        child.terminate()


class CaptureAndTransportTests(unittest.TestCase):
    def bridge(self):
        value = Bridge(None, None)
        value.save_status = lambda: None
        value.event = lambda *a, **kw: None
        return value

    def test_late_stop_from_previous_microphone_does_not_close_new_session(self):
        value = self.bridge()
        value.session.active = True
        value.session.epoch = 8
        value.on_event({'event': 'session_stop', 'epoch': 6})
        self.assertTrue(value.session.active)
        value.on_event({'event': 'session_stop', 'epoch': 8})
        self.assertFalse(value.session.active)

    def test_malformed_device_events_fail_as_protocol_errors(self):
        value = self.bridge()
        for data in [b'[]', b'{"event":"play_done"}', b'{"event":"session_start","epoch":null}']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                value.on_packet(Packet(EVENT, data=data))

    def test_lookahead_rejects_non_monotonic_or_stalled_capture_clock(self):
        value = self.bridge()
        value.session.active = True
        value.session.epoch = 1
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
        value.playback.end_frames = 1
        for old in range(100):
            value.on_event({'event': 'play_done', 'generation': old})
        self.assertEqual(value.playback.completed, set())
        self.assertTrue(value.playback.playing)
        value.on_event({'event': 'play_done', 'generation': value.session.generation})
        self.assertEqual(value.playback.completed, {value.session.generation})
        self.assertFalse(value.playback.playing)

    def test_old_calibration_cannot_replace_new_session_echo_state(self):
        value = self.bridge()
        value.session.active = True
        value.session.connection = object()
        value.calibration.active = True
        state = object()
        value.capture.echo.aec = state
        value.capture.echo.calibration_frames = [('new',)]
        self.assertFalse(value.align_reference(value.session.generation - 1))
        self.assertIs(value.capture.echo.aec, state)
        self.assertEqual(value.capture.echo.calibration_frames, [('new',)])
        self.assertTrue(value.calibration.active)

    def test_calibration_rebuild_failure_leaves_the_working_pair_and_shift(self):
        for failure in ('create_stop_processor', 'warm_stop_processor'):
            with self.subTest(failure=failure):
                value = self.bridge()
                value.session.active = True
                value.session.connection = object()
                value.capture.endpoint.fast_stop = Mock()
                echo = value.capture.echo
                old, old_stop = Mock(), Mock()
                new, new_stop = Mock(), Mock()
                echo.aec, echo.stop_aec = old, old_stop
                echo.reference_shift_ns = 25_000_000
                samples = [(index * 10_000_000, np.zeros(FRAME)) for index in range(100)]
                echo.calibration_frames = list(samples)
                error = RuntimeError('native fixture failed')
                if failure == 'warm_stop_processor':
                    new_stop.process.side_effect = error
                processors = [new, error if failure == 'create_stop_processor' else new_stop]
                with (
                    patch('bridge.WebRtcEchoCanceller', side_effect=processors),
                    patch('echo_pipeline.estimate_reference_shift', return_value=(160, 0.5)),
                    self.assertRaises(RuntimeError),
                ):
                    value.align_reference(value.session.generation)
                old.close.assert_not_called()
                old_stop.close.assert_not_called()
                new.close.assert_called_once()
                self.assertEqual(new_stop.close.call_count, int(failure == 'warm_stop_processor'))
                self.assertIs(echo.aec, old)
                self.assertIs(echo.stop_aec, old_stop)
                self.assertEqual(echo.reference_shift_ns, 25_000_000)
                echo.process(1_000_000_000, np.zeros(FRAME))
                old.process.assert_called_once()
                old_stop.process.assert_called_once()

    def test_partial_native_start_does_not_publish_capture_or_accept_rebind(self):
        value = self.bridge()
        value.session.connection = object()
        value.capture.endpoint.fast_stop = Mock()
        first, retry, retry_stop = Mock(), Mock(), Mock()
        error = RuntimeError('second native processor failed')
        with (
            patch('bridge.WebRtcEchoCanceller', side_effect=[first, error, retry, retry_stop]),
            patch('threading.Thread.start') as start,
        ):
            with self.assertRaises(RuntimeError):
                value.on_event({'event': 'session_start', 'epoch': 7})
            first.close.assert_called_once()
            self.assertFalse(value.session.active)
            self.assertTrue(value.session.capture_start_required)
            self.assertFalse(value.calibration.active)
            self.assertIsNone(value.capture.echo.aec)
            self.assertIsNone(value.capture.echo.stop_aec)
            value.on_event({'event': 'session_rebind', 'epoch': 7, 'capture_active': True})
            self.assertFalse(value.session.active)
            start.assert_not_called()
            value.on_event({'event': 'session_start', 'epoch': 8})
            self.assertTrue(value.session.active)
            self.assertEqual(value.session.epoch, 8)
            self.assertIs(value.capture.echo.aec, retry)
            self.assertIs(value.capture.echo.stop_aec, retry_stop)
            start.assert_called_once()

    def test_negotiated_capture_failure_can_be_resent_on_backup_without_restarting(self):
        value = self.bridge()
        value.session.connection = object()
        value.event = Mock()
        value.session.ready({'capture_reject_version': 1})
        broken_id, fresh_id = 'a' * 32, 'b' * 32
        failed = {'event': 'session_start', 'capture_id': broken_id, 'epoch': 7}
        rebound = {**failed, 'event': 'session_rebind', 'capture_active': True}

        def output(name, **fields):
            if name == 'capture_rejected':
                raise OSError('first link failed while notifying')

        value.event.side_effect = output
        with (
            patch(
                'bridge.WebRtcEchoCanceller', side_effect=[RuntimeError('fixture'), Mock()]
            ) as native,
            patch('threading.Thread.start') as start,
        ):
            with self.assertRaises(OSError):
                value.on_event(failed)
            self.assertFalse(value.session.active)
            value.event.reset_mock(side_effect=True)
            value.change_transport(SimpleNamespace(kind='wifi'))
            self.assertFalse(value.session.capture_rejection_supported)
            value.session.ready({'capture_reject_version': 1, 'capture_active': True})
            for event in (rebound, failed):
                value.on_event(event)
                value.event.assert_called_with('capture_rejected', capture_id=broken_id)
            native.assert_called_once()
            start.assert_not_called()
            value.on_event({**failed, 'capture_id': fresh_id})
            self.assertTrue(value.session.active)
            self.assertEqual(native.call_count, 2)
            start.assert_called_once()
            value.on_event(rebound)
            value.event.assert_called_with('capture_rejected', capture_id=broken_id)
            self.assertTrue(value.session.active)
            self.assertEqual(native.call_count, 2)

    def test_negotiated_capture_id_is_required_and_strict(self):
        value = self.bridge()
        value.session.connection = object()
        value.session.ready({'capture_reject_version': 1})
        for event in ('session_start', 'session_rebind'):
            for fields in ({}, {'capture_id': True}, {'capture_id': 'untrusted'}):
                with self.subTest(event=event, fields=fields), self.assertRaises(ValueError):
                    value.on_event({'event': event, 'epoch': 7, 'capture_active': True, **fields})
        self.assertFalse(value.session.active)
        with self.assertRaises(ValueError):
            value.on_event({'event': 'device_ready', 'capture_reject_version': True})

    def test_cancel_during_alignment_cannot_finish_the_next_calibration(self):
        value = self.bridge()
        value.session.active = True
        value.session.connection = object()
        value.calibration.active = True
        generation = value.session.generation

        def switched(*args):
            value.session.generation += 1
            return True

        with (
            patch.object(value, 'startup_reference', return_value=object()),
            patch.object(value, 'play', return_value=True),
            patch.object(value, 'align_reference', side_effect=switched),
            patch('bridge.time.sleep'),
        ):
            value.calibrate(generation)
        self.assertTrue(value.calibration.active)

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

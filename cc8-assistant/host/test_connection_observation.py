"""Exercise fresh owner IPC, status filtering and legacy unknowns without capture."""

from contextlib import redirect_stdout
import io
import json
import queue
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from announce import AnnouncementServer
from bridge import Bridge
from connection_observation import availability, connection_report
from diagnose import query
import diagnose


def sample(**changes):
    return dict(
        version=1,
        state='connected',
        waiting_ms=0,
        unavailable_ms=0,
        last_recovered_ms=-1,
        recovered_count=0,
        recovered_total_ms=0,
        interrupted_count=0,
        **changes,
    )


class ConnectionObservationTests(unittest.TestCase):
    def test_allowed_samples_are_independent_and_content_free(self):
        for state in ('waiting', 'connected', 'unavailable', 'not_observing'):
            value = {
                **sample(),
                'state': state,
                'summary': 'PRIVATE',
                'unknown': {'text': 'PRIVATE'},
            }
            result = availability(value)
            self.assertEqual(result['state'], state)
            self.assertNotIn('PRIVATE', json.dumps(result))
            result['interrupted_count'] = 2
            self.assertEqual(value['interrupted_count'], 0)
        self.assertEqual(
            availability(
                {**sample(), 'last_recovered_ms': 5, 'recovered_count': 1, 'recovered_total_ms': 8}
            )['last_recovered_ms'],
            5,
        )

    def test_malformed_or_inconsistent_receipts_are_missing(self):
        for change in (
            {'version': True},
            {'version': 2},
            {'state': []},
            {'state': 'private'},
            {'waiting_ms': 3},
            {'unavailable_ms': 5},
            {'last_recovered_ms': 0},
            {'recovered_total_ms': 1},
            {'recovered_count': 1},
            {'recovered_count': 1, 'last_recovered_ms': 9, 'recovered_total_ms': 8},
        ):
            with self.subTest(change=change):
                self.assertIsNone(availability({**sample(), **change}))
        for field in (
            'waiting_ms',
            'unavailable_ms',
            'last_recovered_ms',
            'recovered_count',
            'recovered_total_ms',
            'interrupted_count',
        ):
            for value in (True, None, 'PRIVATE', 2**63, -2):
                self.assertIsNone(availability({**sample(), field: value}))
        for value in (None, [], 'PRIVATE', {}):
            self.assertIsNone(availability(value))

    def test_report_queries_once_and_does_not_reuse_or_invent_missing_data(self):
        control = Mock(return_value={'ok': True, 'connection_availability': sample()})
        result = connection_report(control)
        self.assertEqual(result['observation'], 'device_reply')
        control.assert_called_once_with('status', timeout=3)
        for reply in ({'ok': True}, {'ok': False, 'connection_availability': sample()}):
            self.assertIsNone(
                connection_report(Mock(return_value=reply))['connection_availability']
            )
        for error in (RuntimeError('PRIVATE'), EOFError('PRIVATE')):
            result = connection_report(Mock(side_effect=error))
            self.assertEqual(result['observation'], 'unavailable')
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_real_bridge_status_callback_filters_content_and_never_starts_capture(self):
        with patch('threading.Thread.start'):
            bridge = Bridge(None, None)
        bridge.session.connection = object()
        sent = []

        def reply(name, **fields):
            sent.append((name, fields))
            bridge.on_event(
                {
                    'event': 'device_control_result',
                    'request_id': fields['request_id'],
                    'ok': True,
                    'connection_availability': {**sample(), 'summary': 'PRIVATE'},
                }
            )

        bridge.event = reply
        result = bridge.connection_diagnostics()
        self.assertEqual(result['observation'], 'device_reply')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]['action'], 'status')
        self.assertEqual(bridge.device.waiters, {})
        self.assertFalse(bridge.session.active)

    def test_real_owner_ipc_reports_disconnected_without_device_command(self):
        bridge = Bridge(None, None)
        bridge.event = Mock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'owner.sock'
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                report = query(path, 'connection_diagnostics')
                self.assertEqual(report['observation'], 'unavailable')
                self.assertIsNone(report['connection_availability'])
            finally:
                server.close()
        bridge.event.assert_not_called()
        self.assertFalse(bridge.session.active)

    def test_cli_explicit_export_is_private_and_mutually_exclusive_with_health(self):
        report = connection_report(
            Mock(return_value={'ok': True, 'connection_availability': sample()})
        )
        with tempfile.TemporaryDirectory() as folder, patch.object(diagnose, 'ROOT', Path(folder)):
            stdout = io.StringIO()
            with (
                patch.object(diagnose, 'query', return_value=report) as query_mock,
                patch('sys.argv', ['diagnose.py', '--connection']),
                redirect_stdout(stdout),
            ):
                diagnose.main()
            query_mock.assert_called_once_with(operation='connection_diagnostics')
            path = Path(stdout.getvalue().strip())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), report)
            with (
                patch.object(diagnose, 'query') as query_mock,
                patch('sys.argv', ['diagnose.py', '--connection', '--health']),
                patch('sys.stderr', io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                diagnose.main()
            query_mock.assert_not_called()

    def test_invalid_timeout_fails_before_any_device_command(self):
        with patch('threading.Thread.start'):
            bridge = Bridge(None, None)
        bridge.event = Mock()
        for timeout in (True, 0, -1, float('nan'), float('inf'), 6, 'PRIVATE'):
            with self.assertRaises(ValueError):
                bridge.device.control('status', timeout=timeout)
        bridge.event.assert_not_called()

    def test_fresh_query_timeout_releases_waiter_without_resubmission(self):
        with patch('threading.Thread.start'):
            bridge = Bridge(None, None)
        bridge.session.connection = object()
        bridge.event = Mock()
        waiter = Mock()
        waiter.get.side_effect = queue.Empty
        with patch('device_controller.queue.Queue', return_value=waiter):
            self.assertEqual(bridge.connection_diagnostics()['observation'], 'unavailable')
        waiter.get.assert_called_once_with(timeout=3)
        bridge.event.assert_called_once()
        self.assertEqual(bridge.device.waiters, {})
        self.assertFalse(bridge.session.active)

    def test_owner_ipc_rejects_extra_actions_before_device_dispatch(self):
        bridge = Bridge(None, None)
        bridge.connection_diagnostics = Mock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'owner.sock'
            server = AnnouncementServer(bridge, path)
            server.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(path))
                    client.sendall(b'{"operation":"connection_diagnostics","action":"start"}\n')
                    self.assertEqual(json.loads(client.recv(4096)), {'status': 'invalid_request'})
            finally:
                server.close()
        bridge.connection_diagnostics.assert_not_called()
        self.assertFalse(bridge.session.active)


if __name__ == '__main__':
    unittest.main()

"""Real TLS reaches ASR/TTS controllers without owner credentials or providers."""

from collections import Counter
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

from tools.check_failure_ui import corpus_identity
from tools.qa_faults import CASES, Case, generate
from tools.qa_http import LoopbackHTTP


class HTTPFixtureTests(unittest.TestCase):
    def test_tls_and_dot_stdio_run_together_without_extra_provider_requests(self):
        rows = generate(http_fixture='loopback_tls', dot_fixture='local_stdio')
        self.assertEqual(len(rows), 48)
        self.assertEqual(sum(row['wire_requests'] for row in rows), 30)
        self.assertEqual(sum(row['recovery_wire_requests'] for row in rows), 30)
        self.assertEqual(sum(row['dot_wire_counts']['fault_turns'] for row in rows), 33)
        self.assertEqual(sum(row['dot_wire_counts']['recovery_turns'] for row in rows), 48)
        self.assertEqual(sum(row['dot_wire_counts']['declined_approvals'] for row in rows), 3)
        self.assertEqual(
            Counter(row['case'].stage for row in rows), {'asr': 15, 'brain': 18, 'tts': 15}
        )
        from protocol import decode_event

        for row in rows:
            self.assertEqual(row['http_fixture'], 'loopback_tls')
            expected = int(row['case'].stage in ('asr', 'tts'))
            self.assertEqual(row['wire_requests'], expected)
            self.assertEqual(row['recovery_wire_requests'], expected)
            errors = [decode_event(p.data) for p in row['error']]
            messages = [e for e in errors if e.get('phase') == 'error']
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]['message'], row['expected_message'])
            self.assertNotIn('qa-injected-sensitive-body', json.dumps(errors))

    def test_selected_cases_make_no_unselected_provider_requests(self):
        selected = (Case('asr', 'rate_limit', 1), Case('tts', 'quota', 2))
        rows = generate(http_fixture='loopback_tls', cases=selected)
        self.assertEqual(tuple(row['case'] for row in rows), selected)
        self.assertEqual([row['wire_requests'] for row in rows], [1, 1])
        self.assertEqual([row['recovery_wire_requests'] for row in rows], [1, 1])
        self.assertNotEqual(
            corpus_identity(rows),
            corpus_identity([{**rows[0], 'recovery_wire_requests': 0}, rows[1]]),
        )

    def test_business_faults_and_next_http_request_recover_for_each_case(self):
        rows = generate(http_fixture='loopback_business', dot_fixture='local_stdio')
        self.assertEqual(len(rows), 48)
        self.assertEqual(sum(row['wire_requests'] for row in rows), 30)
        self.assertEqual(sum(row['recovery_wire_requests'] for row in rows), 30)
        self.assertEqual(sum(row['dot_wire_counts']['fault_turns'] for row in rows), 33)
        self.assertEqual(sum(row['dot_wire_counts']['recovery_turns'] for row in rows), 48)
        self.assertEqual(sum(row['dot_wire_counts']['declined_approvals'] for row in rows), 3)
        for row in rows:
            expected = int(row['case'].stage in ('asr', 'tts'))
            self.assertEqual(row['wire_requests'], expected)
            self.assertEqual(row['recovery_wire_requests'], expected)

    def test_business_fixture_serves_nonzero_http_200_before_a_distinct_success(self):
        recovery = [False]
        with LoopbackHTTP(
            lambda: Case('tts', 'quota', 2),
            lambda: None,
            business=True,
            recovering=lambda: recovery[0],
        ) as fixture:
            with fixture.session_factory() as client:
                for is_recovery in (False, True):
                    recovery[0] = is_recovery
                    response = client.post(
                        fixture.base + '/v1/t2a_v2',
                        json={'text': 'fixed'},
                        headers={'Authorization': 'Bearer qa-not-a-real-credential'},
                        timeout=2,
                    )
                    with response:
                        self.assertEqual(response.status_code, 200)
                        self.assertIn('text/event-stream', response.headers['Content-Type'])
                        event = json.loads(response.content.splitlines()[0][len(b'data: ') :])
                        if is_recovery:
                            self.assertEqual(event['data']['status'], 2)
                            self.assertTrue(event['data']['audio'])
                        else:
                            self.assertEqual(event['base_resp']['status_code'], 1008)
            self.assertEqual(fixture.count('tts-quota-2'), 1)
            self.assertEqual(fixture.count('tts-quota-2', recovery=True), 1)

    def test_unknown_or_duplicate_cases_are_rejected_before_network_or_process(self):
        for selection in (
            (),
            [CASES[0]],
            ({},),
            (Case('asr', 'timeout', True),),
            (CASES[0], CASES[0]),
        ):
            with (
                self.subTest(selection_type=type(selection).__name__),
                patch.object(socket.socket, 'connect') as connect,
                patch.object(subprocess, 'Popen') as process,
            ):
                with self.assertRaises(ValueError):
                    generate(http_fixture='loopback_tls', cases=selection)
                connect.assert_not_called()
                process.assert_not_called()

    def test_peer_certificate_is_verified_and_fixture_post_cannot_contact_remote_hosts(self):
        requests_seen = []
        with LoopbackHTTP(
            lambda: Case('tts', 'rate_limit', 1), lambda: requests_seen.append(True)
        ) as fixture:
            with requests.Session() as untrusted:
                untrusted.trust_env = False
                with self.assertRaises(requests.exceptions.SSLError):
                    untrusted.post(fixture.base + '/v1/t2a_v2', json={'text': 'fixed'}, timeout=2)
            with fixture.session_factory() as trusted:
                response = trusted.post(
                    fixture.base + '/v1/t2a_v2',
                    json={'text': 'fixed'},
                    headers={'Authorization': 'Bearer qa-not-a-real-credential'},
                    timeout=2,
                )
                self.assertEqual(response.status_code, 429)
                response.close()
                with patch.object(socket.socket, 'connect') as connect:
                    with self.assertRaises(AssertionError):
                        trusted.post('https://api.minimax.cn/v1/t2a_v2', json={})
                    with self.assertRaises(AssertionError):
                        trusted.post(fixture.base + '/v1/t2a_v2', json={}, allow_redirects=True)
                    connect.assert_not_called()
            self.assertEqual(requests_seen, [True])
            self.assertEqual(fixture.count('tts-rate_limit-1'), 1)
        self.assertFalse(fixture.worker.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in fixture.handlers))

    def test_certificate_preparation_failure_cleans_up_before_listening(self):
        fixture = LoopbackHTTP(lambda: None, lambda: None)
        with patch(
            'tools.qa_http.subprocess.run', side_effect=subprocess.TimeoutExpired('openssl', 1)
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                fixture.__enter__()
        self.assertFalse(Path(fixture.folder.name).exists())
        self.assertIsNone(fixture.worker)
        self.assertIsNone(fixture.listener)

    def test_fixture_session_rejects_a_different_peer_certificate(self):
        def current_case():
            return Case('tts', 'rate_limit', 1)

        with (
            LoopbackHTTP(current_case, lambda: None) as fixture,
            LoopbackHTTP(current_case, lambda: None) as other,
        ):
            # Keep the client's expected URL/CA unchanged, but serve a different
            # valid localhost certificate. verify=False would incorrectly pass.
            fixture.context = other.context
            with fixture.session_factory() as client:
                with self.assertRaises(requests.exceptions.SSLError):
                    client.post(
                        fixture.base + '/v1/t2a_v2',
                        json={'text': 'fixed'},
                        headers={'Authorization': 'Bearer qa-not-a-real-credential'},
                        timeout=2,
                    )
            self.assertEqual(fixture.count('tts-rate_limit-1'), 0)

    def test_stdlib_minimal_path_tls_plan_does_not_create_runtime_or_open_network(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            absent = Path(folder) / 'absent'
            result = subprocess.run(
                [
                    sys.executable,
                    '-S',
                    str(root / 'cc8'),
                    'verify-failure-ui',
                    '--runtime-root',
                    str(absent),
                    '--http-fixture',
                    'loopback_tls',
                    '--dot-fixture',
                    'local_stdio',
                ],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertFalse(plan['execute'])
            self.assertEqual(plan['provider_http_fixture'], 'loopback_tls')
            self.assertEqual(plan['provider_dot_fixture'], 'local_stdio')
            self.assertFalse(plan['real_provider_requested'])
            self.assertFalse(absent.exists())


if __name__ == '__main__':
    unittest.main()

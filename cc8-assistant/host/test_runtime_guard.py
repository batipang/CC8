"""Cross-checkout ownership without touching the owner's live runtime or lock."""

import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import runtime_guard as guard
from process_lock import ProcessLease


class RuntimeGuardTests(unittest.TestCase):
    def test_process_scan_preserves_spaces_filters_uid_and_supervisor_actions(self):
        text = '\n'.join(
            [
                '501 12 /app/Python -u /vol/CC8 Test/cc8-assistant/host/bridge.py',
                '501 13 /bin/python3.13 /vol/Other/cc8-assistant/host/service.py run',
                '501 14 /bin/python3 /ignored/cc8-assistant/host/service.py status',
                '999 15 /bin/python3 /ignored/cc8-assistant/host/bridge.py',
                '501 16 /bin/sh -c echo /ignored/cc8-assistant/host/bridge.py',
                'broken',
            ]
        )
        self.assertEqual(
            guard.process_roots(text, uid=501), {Path('/vol/CC8 Test'), Path('/vol/Other')}
        )

    def test_process_scan_failure_is_closed_and_sanitized(self):
        with patch.object(guard.subprocess, 'run', side_effect=OSError('secret command')):
            with self.assertRaises(guard.RuntimeConflict) as error:
                guard.active_roots()
        self.assertNotIn('secret', str(error.exception))

    def test_enabled_launchagent_reserves_backoff_but_disabled_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'checkout'
            agents = Path(folder) / 'LaunchAgents'
            agents.mkdir()
            path = agents / 'com.happyhome.cc8.example.plist'
            path.write_bytes(
                plistlib.dumps(
                    {
                        'ProgramArguments': [
                            '/bin/python',
                            str(root / 'cc8-assistant/host/service.py'),
                            'run',
                        ]
                    }
                )
            )
            path.chmod(0o600)
            marker = root / 'device-access/private/voice-runtime/service-enabled.json'
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps({'enabled': True}))
            marker.chmod(0o600)
            self.assertEqual(guard.enabled_service_roots(agents), {root.resolve()})
            marker.write_text(json.dumps({'enabled': False}))
            self.assertEqual(guard.enabled_service_roots(agents), set())
            marker.write_text(json.dumps({'enabled': True}))
            marker.chmod(0o644)
            self.assertEqual(guard.enabled_service_roots(agents), set())

    def test_other_checkout_is_never_stopped_and_releases_failed_lease(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lease = root / 'owner/lease'
            with patch.object(guard, 'other_runtime', return_value=True):
                with self.assertRaises(guard.RuntimeConflict):
                    with guard.RuntimeLease(root / 'checkout', lease):
                        self.fail('must not start')
            with ProcessLease(lease):
                pass
            self.assertFalse((root / 'checkout').exists())

    def test_real_children_in_different_checkouts_share_one_os_lease_and_release_on_crash(self):
        code = """import sys,time
from pathlib import Path
from unittest.mock import patch
from runtime_guard import RuntimeLease
with patch('runtime_guard.ensure_available'):
 try:
  with RuntimeLease(Path(sys.argv[1]), Path(sys.argv[2])):
   print('owned',flush=True);time.sleep(30)
 except BlockingIOError: print('refused',flush=True)
"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lease = root / 'owner/lease'
            first = subprocess.Popen(
                [sys.executable, '-c', code, str(root / 'a'), str(lease)],
                cwd=Path(__file__).parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(first.stdout.readline().strip(), 'owned')
                second = subprocess.run(
                    [sys.executable, '-c', code, str(root / 'b'), str(lease)],
                    cwd=Path(__file__).parent,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(second.stdout.strip(), 'refused')
            finally:
                first.kill()
                first.wait(timeout=5)
                first.stdout.close()
                first.stderr.close()
            with patch.object(guard, 'ensure_available'):
                with guard.RuntimeLease(root / 'b', lease):
                    pass


if __name__ == '__main__':
    unittest.main()

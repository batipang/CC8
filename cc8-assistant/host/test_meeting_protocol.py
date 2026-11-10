"""Meeting framing controls stay usable without any audio/model/provider imports."""

from pathlib import Path
import subprocess
import sys
import unittest


class MeetingProtocolTests(unittest.TestCase):
    def test_fresh_protocol_decode_never_imports_audio_provider_or_state_modules(self):
        host = Path(__file__).resolve().parent
        script = """
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
blocked = {'numpy', 'requests', 'minimax_client', 'meeting_asr', 'meeting_session', 'meeting_view', 'configuration'}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('Protocol imported a provider/state dependency')
sys.meta_path.insert(0, Guard())
from protocol import decode_event
value = decode_event(b'{"event":"meeting_request","request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","action":"status"}')
assert value['action'] == 'status'
assert not blocked.intersection(sys.modules)
print('PASS')
"""
        result = subprocess.run(
            [sys.executable, '-I', '-c', script, str(host)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'PASS')


if __name__ == '__main__':
    unittest.main()

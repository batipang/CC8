"""Use isolated real processes to verify app-server shutdown with tool descendants."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from dot_client import CodexRpc, DotError


TOOL = """
import os, signal, sys, time
from pathlib import Path
if sys.argv[2] != 'cooperative':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(20)
"""
SERVER = textwrap.dedent("""
    import json, os, subprocess, sys, time
    from pathlib import Path

    path = Path(sys.argv[1])
    mode = sys.argv[2]
    tool = subprocess.Popen(
        [sys.executable, str(path.with_name('tool.py')), str(path), mode],
        stdin=subprocess.DEVNULL,
        stdout=None if mode == 'leader_exit' else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    until = time.monotonic() + 3
    while not path.exists():
        if time.monotonic() >= until or tool.poll() is not None:
            raise SystemExit(1)
        time.sleep(.01)
    for line in sys.stdin:
        message = json.loads(line)
        if message.get('method') == 'initialize':
            print(json.dumps({'id':message['id'],'result':{}}), flush=True)
        elif message.get('method') == 'initialized' and mode == 'leader_exit':
            os._exit(7)
    """)


def process_state(pid):
    result = subprocess.run(
        ['/bin/ps', '-p', str(pid), '-o', 'stat='],
        capture_output=True,
        text=True,
        timeout=1,
    )
    return result.stdout.strip()


def alive(pid):
    state = process_state(pid)
    return bool(state) and not state.startswith('Z')


class DotProcessGroupTests(unittest.TestCase):
    def test_close_drains_owned_group_after_leader_exit_or_term(self):
        for mode in ('cooperative', 'resistant', 'leader_exit'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                script = Path(folder) / 'server.py'
                path = Path(folder) / 'tool.pid'
                script.write_text(SERVER)
                script.with_name('tool.py').write_text(TOOL)
                rpc = CodexRpc(command=[sys.executable, '-u', str(script), str(path), mode])
                tool = None
                try:
                    with patch(
                        'dot_client.runtime_environment', return_value={'PATH': '/usr/bin:/bin'}
                    ):
                        rpc.start()
                    tool = int(path.read_text())
                    self.assertEqual(os.getpgid(tool), rpc.process.pid)
                    self.assertTrue(alive(tool))
                    if mode == 'cooperative':
                        original = rpc.process
                        rpc.start()
                        self.assertIs(rpc.process, original)
                    if mode == 'leader_exit':
                        deadline = time.monotonic() + 2
                        while alive(rpc.process.pid) and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertFalse(alive(rpc.process.pid))
                        # The surviving child owns stdout, so EOF cannot be the
                        # liveness signal for this case.
                        self.assertFalse(rpc.failed)
                        with self.assertRaises(DotError):
                            rpc.start()
                    started = time.monotonic()
                    rpc.close()
                    self.assertLess(time.monotonic() - started, 4)
                    self.assertIsNotNone(rpc.process.poll())
                    deadline = time.monotonic() + 1
                    while alive(tool) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(alive(tool), 'Owned tool survived RPC shutdown')
                    rpc.close()
                    self.assertFalse(rpc.initialized)
                finally:
                    if tool is None and path.exists():
                        tool = int(path.read_text())
                    if tool is not None and alive(tool) and os.getpgid(tool) == rpc.process.pid:
                        os.kill(tool, signal.SIGKILL)
                    if rpc.process is not None:
                        if rpc.process.poll() is None:
                            rpc.process.kill()
                            rpc.process.wait(timeout=2)
                        for stream in (rpc.process.stdin, rpc.process.stdout, rpc.process.stderr):
                            stream.close()

    def test_closed_client_cannot_start_a_process(self):
        with patch(
            'dot_client.subprocess.Popen', side_effect=DotError('Unexpected spawn')
        ) as spawn:
            rpc = CodexRpc(command=['unused'], popen=spawn)
            rpc.close()
            with self.assertRaises(DotError):
                rpc.start()
            spawn.assert_not_called()

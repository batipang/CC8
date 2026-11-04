"""Use isolated real processes to verify app-server shutdown with tool descendants."""

import json
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
        elif message.get('method') == 'finish' and mode == 'leader_exit':
            print(json.dumps({'id':message['id'],'result':{'completed':True}}), flush=True)
            os._exit(7)
    """)
OWNER = textwrap.dedent("""
    import json, os, sys, time
    from pathlib import Path
    from unittest.mock import patch

    sys.path.insert(0, sys.argv[1])
    from dot_client import CodexRpc
    root = Path(sys.argv[2])
    path = root / 'tool.pid'
    rpc = CodexRpc(command=[sys.executable, '-u', str(root / 'server.py'), str(path), 'resistant'])
    with patch('dot_client.runtime_environment', return_value={'PATH': '/usr/bin:/bin'}):
        rpc.start()
    tool = int(path.read_text())
    receipt = {'guardian': rpc.process.pid, 'group': os.getpgid(tool), 'tool': tool}
    temporary = root / 'pending.json'
    temporary.write_text(json.dumps(receipt))
    temporary.replace(root / 'owned.json')
    time.sleep(20)
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


def wait_until(condition, timeout=4):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


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
                group = None
                try:
                    with patch(
                        'dot_client.runtime_environment', return_value={'PATH': '/usr/bin:/bin'}
                    ):
                        rpc.start()
                    tool = int(path.read_text())
                    group = os.getpgid(tool)
                    self.assertEqual(os.getpgid(group), group)
                    self.assertTrue(alive(tool))
                    if mode == 'cooperative':
                        original = rpc.process
                        rpc.start()
                        self.assertIs(rpc.process, original)
                    if mode == 'leader_exit':
                        self.assertEqual(rpc.request('finish', {}, timeout=2), {'completed': True})
                        self.assertTrue(wait_until(lambda: not alive(group), timeout=2))

                        def unavailable():
                            try:
                                rpc.start()
                            except DotError:
                                return True
                            return False

                        self.assertTrue(wait_until(unavailable, timeout=1.5))
                        self.assertTrue(
                            wait_until(lambda: not alive(tool)),
                            'Tool survived an app-server exit before host close',
                        )
                        self.assertTrue(wait_until(lambda: rpc.process.poll() is not None))
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
                    rpc.close()
                    if tool is None and path.exists():
                        tool = int(path.read_text())
                    if tool is not None and group is None and alive(tool):
                        group = os.getpgid(tool)
                    if (
                        tool is not None
                        and group is not None
                        and alive(tool)
                        and os.getpgid(tool) == group
                    ):
                        os.kill(tool, signal.SIGKILL)
                    if rpc.process is not None:
                        if rpc.process.poll() is None:
                            rpc.process.kill()
                            rpc.process.wait(timeout=2)
                        for stream in (rpc.process.stdin, rpc.process.stdout, rpc.process.stderr):
                            stream.close()

    def test_owner_sigkill_drains_real_guardian_and_tool_without_host_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'server.py').write_text(SERVER)
            (root / 'tool.py').write_text(TOOL)
            owner = subprocess.Popen(
                [sys.executable, '-c', OWNER, str(Path(__file__).resolve().parent), folder],
                cwd='/',
                env={'PATH': '/usr/bin:/bin'},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            ids = None
            try:
                path = root / 'owned.json'
                self.assertTrue(wait_until(lambda: path.exists() or owner.poll() is not None))
                self.assertIsNone(owner.poll())
                ids = json.loads(path.read_text())
                self.assertEqual(os.getpgid(ids['tool']), ids['group'])
                self.assertTrue(all(alive(pid) for pid in ids.values()))
                owner.kill()
                self.assertEqual(owner.wait(timeout=2), -signal.SIGKILL)
                self.assertTrue(
                    wait_until(lambda: not any(alive(pid) for pid in ids.values())),
                    'App-server, tool or guardian survived owner SIGKILL',
                )
                self.assertNotEqual(ids['guardian'], ids['group'])
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=2)
                if ids is not None:
                    for key, pid in ids.items():
                        expected_group = ids['guardian' if key == 'guardian' else 'group']
                        if alive(pid) and os.getpgid(pid) == expected_group:
                            os.kill(pid, signal.SIGKILL)

    def test_closed_client_cannot_start_a_process(self):
        with patch(
            'dot_client.subprocess.Popen', side_effect=DotError('Unexpected spawn')
        ) as spawn:
            rpc = CodexRpc(command=['unused'], popen=spawn)
            rpc.close()
            with self.assertRaises(DotError):
                rpc.start()
            spawn.assert_not_called()

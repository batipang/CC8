"""Verify an in-flight side effect survives a real supervised backend crash once."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import diagnose
import launch
import service
from process_lock import atomic_private_json
from test_dot_process_group import alive, wait_until
from test_supervisor_process import fixture_owned

HOST = Path(__file__).resolve().parent
INTERRUPTED = '固定故障请求'
FRESH = '固定恢复请求'


def fixture_tool(root):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with (root / 'effects').open('a') as journal:
        journal.write('applied\n')
    atomic_private_json(root / 'effect-process.json', {'pid': os.getpid(), 'group': os.getpgrp()})
    time.sleep(25)


def fixture_peer(root):
    def send(value):
        print(json.dumps(value), flush=True)

    for line in sys.stdin:
        request = json.loads(line)
        method = request['method']
        if method == 'initialized':
            continue
        ident = request['id']
        result = {}
        if method == 'thread/start':
            result = {'thread': {'id': 'fixture-thread'}}
        elif method == 'turn/start':
            prompt = request['params']['input'][0]['text'].rsplit('\n', 1)[-1]
            if prompt not in (INTERRUPTED, FRESH):
                raise ValueError('Unexpected fixture request')
            action = 'interrupted' if prompt == INTERRUPTED else 'fresh'
            with (root / 'submissions').open('a') as journal:
                journal.write(action + '\n')
            turn = 'fixture-' + action
            scope = {'threadId': 'fixture-thread', 'turnId': turn}
            send({'id': ident, 'result': {'turn': {'id': turn}}})
            if action == 'interrupted':
                # Deliberately survives TERM, so only the guardian can finish
                # cleanup after the backend owning the RPC socket is killed.
                subprocess.Popen(
                    [sys.executable, '-B', str(Path(__file__).resolve()), '--tool', str(root)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                atomic_private_json(root / 'guardian.json', {'pid': os.getppid()})
                continue
            send(
                {
                    'method': 'item/completed',
                    'params': {
                        **scope,
                        'item': {
                            'id': 'fixture-answer',
                            'type': 'agentMessage',
                            'phase': 'final_answer',
                            'text': '固定恢复请求已完成。',
                        },
                    },
                }
            )
            send(
                {
                    'method': 'turn/completed',
                    'params': {**scope, 'turn': {'id': turn, 'status': 'completed'}},
                }
            )
            continue
        elif method not in ('initialize', 'turn/interrupt'):
            raise ValueError('Unexpected fixture RPC')
        send({'id': ident, 'result': result})


def fixture_backend(root):
    from announce import AnnouncementServer
    from brain_router import BrainRouter
    import bridge
    from dot_client import CodexRpc, DotBrain

    class Link:
        kind = 'usb'

        def sendall(self, packet):
            pass

    class InactiveProvider:
        def cancel(self):
            pass

    private = root / 'p'
    bridge.PRIVATE = private
    bridge.dot_runtime_status = lambda: {'fixture': True}
    rpc = CodexRpc(
        command=[sys.executable, '-B', str(Path(__file__).resolve()), '--peer', str(root)]
    )
    b = bridge.Bridge(None, BrainRouter(InactiveProvider(), DotBrain(rpc)))
    b.session.connection = Link()
    b.speak = lambda *_: True  # Exercise the real task path without playback.
    b.save_status = lambda: None
    server = AnnouncementServer(b, private / 'announce.sock')
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with patch('codex_rpc.runtime_environment', return_value={'PATH': '/usr/bin:/bin'}):
        try:
            server.start()
            stop.wait(30)
        finally:
            server.close()
            rpc.close()


def submit(path, prompt):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(str(path))
        conn.sendall((json.dumps({'operation': 'text_request', 'text': prompt}) + '\n').encode())
        with conn.makefile('rb') as reader:
            return json.loads(reader.readline(65536))


class InFlightRecoveryTests(unittest.TestCase):
    def test_supervisor_does_not_replay_interrupted_task_and_accepts_new_request(self):
        with tempfile.TemporaryDirectory(prefix='cc8-inflight-', dir='/tmp') as directory:
            root = Path(directory)
            private = root / 'p'
            private.mkdir(mode=0o700)
            sock = private / 'announce.sock'
            marker = private / 'enabled.json'
            atomic_private_json(marker, {'enabled': True})
            script = root / 'bridge.py'
            script.write_text(
                f'import sys\nsys.path.insert(0, {str(HOST)!r})\n'
                'from pathlib import Path\n'
                'from test_service_inflight_recovery import fixture_backend\n'
                'fixture_backend(Path(__file__).resolve().parent)\n'
            )
            read_enabled = service.enabled
            errors = []
            with (
                patch.object(service, 'ROOT', root),
                patch.object(service, 'PRIVATE', private),
                patch.object(service, 'BRIDGE', script),
                patch.object(service, 'PYTHON', Path(sys.executable)),
                patch.object(launch, 'BRIDGE', script),
                patch.object(service, 'enabled', side_effect=lambda: read_enabled(marker)),
                patch.object(service, 'ensure_available'),
                patch.object(
                    service, 'query', side_effect=lambda operation: diagnose.query(sock, operation)
                ),
            ):
                supervisor = service.Supervisor()

                def supervise():
                    try:
                        supervisor.run()
                    except Exception as error:
                        errors.append(error)

                def ready(previous=None):
                    try:
                        health = diagnose.query(sock, 'health', timeout=0.2)
                        state = json.loads((private / 'supervisor.json').read_text())
                    except (OSError, ValueError):
                        return None
                    if (
                        state['phase'] == 'ready'
                        and fixture_owned(state['pid'], script)
                        and health['runtime_instance'] != previous
                    ):
                        return state['pid'], health
                    return None

                worker = threading.Thread(target=supervise, daemon=True)
                worker.start()
                tool = None
                try:
                    self.assertTrue(wait_until(ready, timeout=8))
                    pid, initial = ready()
                    self.assertFalse(initial['microphone_active'])
                    self.assertIs(submit(sock, INTERRUPTED).get('ok'), True)
                    effect = root / 'effect-process.json'
                    guardian_path = root / 'guardian.json'
                    self.assertTrue(
                        wait_until(lambda: effect.is_file() and guardian_path.is_file())
                    )
                    tool = json.loads(effect.read_text())
                    guardian = json.loads(guardian_path.read_text())['pid']
                    owned_group = (guardian, tool['group'], tool['pid'])
                    self.assertEqual(len(set(owned_group)), 3)
                    self.assertTrue(all(alive(member) for member in owned_group))
                    pending = diagnose.query(sock, 'workspace_status')['tasks']
                    self.assertEqual([task['status'] for task in pending], ['running'])
                    self.assertTrue(fixture_owned(pid, script))
                    os.kill(pid, signal.SIGKILL)

                    previous = initial['runtime_instance']
                    self.assertTrue(wait_until(lambda: ready(previous), timeout=10))
                    replacement, current = ready(previous)
                    self.assertNotEqual(replacement, pid)
                    self.assertFalse(current['microphone_active'])
                    self.assertTrue(
                        wait_until(lambda: not any(alive(member) for member in owned_group))
                    )
                    self.assertEqual(diagnose.query(sock, 'workspace_status')['tasks'], [])
                    self.assertEqual(
                        (root / 'submissions').read_text().splitlines(), ['interrupted']
                    )
                    self.assertEqual((root / 'effects').read_text().splitlines(), ['applied'])

                    self.assertIs(submit(sock, FRESH).get('ok'), True)

                    def completed():
                        tasks = diagnose.query(sock, 'workspace_status')['tasks']
                        return [task['status'] for task in tasks] == ['completed']

                    self.assertTrue(wait_until(completed))
                    self.assertTrue(
                        wait_until(
                            lambda: (
                                [row['outcome'] for row in diagnose.query(sock)['requests']]
                                == ['completed']
                            )
                        )
                    )
                    self.assertEqual(
                        (root / 'submissions').read_text().splitlines(), ['interrupted', 'fresh']
                    )
                    self.assertEqual((root / 'effects').read_text().splitlines(), ['applied'])
                    self.assertFalse(diagnose.query(sock, 'health')['microphone_active'])
                finally:
                    supervisor.stop.set()
                    worker.join(10)
                    if tool and alive(tool['pid']) and os.getpgid(tool['pid']) == tool['group']:
                        os.kill(tool['pid'], signal.SIGKILL)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(
                    json.loads((private / 'supervisor.json').read_text())['phase'], 'stopped'
                )


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] in ('--tool', '--peer'):
        role = fixture_tool if sys.argv[1] == '--tool' else fixture_peer
        role(Path(sys.argv[2]))
    else:
        unittest.main()

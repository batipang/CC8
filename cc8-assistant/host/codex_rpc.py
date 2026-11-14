"""Own one Codex stdio process, its RPC waiters, event queue and cleanup."""

from __future__ import annotations
from collections import deque
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from dot_runtime import codex_command
from runtime_env import runtime_environment
from release_info import RELEASE
from rpc_pipe import RpcPipeWriter, deadline_after
from rpc_process import close_guardian, start_guardian
from dot_errors import error_notice, turn_completion
from dot_receipt import DotError, UnconfirmedTurnStart
from lifecycle_cleanup import cleanup_all

ROOT = Path(__file__).resolve().parents[2]


class EventQueue(queue.Queue):
    """Coalesce text deltas while playback applies backpressure to the reader."""

    def put_nowait(self, item):
        with self.not_full:
            if item.get('method') == 'item/agentMessage/delta' and self.queue:
                last = self.queue[-1]
                a = last.get('params', {})
                b = item.get('params', {})
                if last.get('method') == item['method'] and all(
                    a.get(k) == b.get(k) for k in ('threadId', 'turnId', 'itemId')
                ):
                    text = a.get('delta', '') + b.get('delta', '')
                    if len(text) > 64000:
                        raise DotError('Codex answer exceeds size limit')
                    a['delta'] = text
                    return
            if self.maxsize > 0 and self._qsize() >= self.maxsize:
                raise queue.Full
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()


class CodexRpc:
    # 独立 app-server 的 JSON-RPC 适配，请求回执与异步轮次事件分别分发。
    MAX_LINE = 8 * 1024 * 1024

    def __init__(self, command=None, popen=subprocess.Popen):
        self.command = command
        self.popen = popen
        self.process = None
        self.stdout_reader = None
        self.stderr_reader = None
        self.lifecycle_lock = threading.Lock()
        self.guardian = None
        self.guardian_ending = threading.Event()
        self.lock = threading.Lock()
        self.writer = RpcPipeWriter()
        self.pending = {}
        self.sequence = 0
        self.tool_handler = None
        self.events = EventQueue(maxsize=256)
        self.failed = False
        self.initialized = False
        self.approval_seen = threading.Event()
        self.approval_lock = threading.Lock()
        self.approval_scopes = deque(maxlen=128)
        self.approval_terminals = deque(maxlen=128)
        self.notification_lock = threading.RLock()
        self.listeners = []

    def listen(self, listener):
        with self.notification_lock:
            self.listeners.append(listener)

        def remove():
            with self.notification_lock:
                if listener in self.listeners:
                    self.listeners.remove(listener)

        return remove

    def _receive(self, message):
        if message.get('method') == 'error':
            message = error_notice(message)
            if message is None:
                return
        elif message.get('method') == 'turn/completed':
            message = turn_completion(message)
            self._finish_approval(message)
        # Linearize receipt admission before a speech consumer can see this
        # event. No Queue mutex is held across owner callbacks. Unsubscribe
        # waits for an in-flight admission, so generator cleanup cannot drop it.
        with self.notification_lock:
            for listener in tuple(self.listeners):
                listener(message)
            self.events.put_nowait(message)

    def start(self, *, timeout=20):
        deadline = deadline_after(timeout)

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise DotError('Codex initialization timed out', kind='timeout')
            return budget

        with self.lifecycle_lock:
            if self.failed or self.guardian_ending.is_set():
                self.failed = True
                raise DotError('Codex process is no longer connected', kind='transport_or_io')
            if self.process is not None:
                return
            environment = runtime_environment()
            environment.pop('CC8_MINIMAX_API_KEY', None)
            self.process, self.guardian = start_guardian(
                self.popen,
                self.command or codex_command(),
                cwd=ROOT,
                env=environment,
            )
            lifetime = self.guardian
            process = self.process

        def observe_guardian():
            try:
                lifetime.recv(1)
            except OSError:
                pass
            finally:
                # The stdout reader still owns delivery of already queued
                # terminal receipts and its normal disconnect notification.
                self.guardian_ending.set()

        def drain():
            try:
                while process.stderr.read(8192):
                    pass
            except (OSError, ValueError):
                pass
            finally:
                process.stderr.close()

        try:
            with self.lifecycle_lock:
                if self.failed or lifetime is not self.guardian:
                    raise DotError('Codex startup was cancelled', kind='transport_or_io')
                self.stderr_reader = threading.Thread(target=drain, daemon=True)
                self.stdout_reader = threading.Thread(
                    target=self._read, args=(process,), daemon=True
                )
                self.stderr_reader.start()
                self.stdout_reader.start()
                threading.Thread(target=observe_guardian, daemon=True).start()
            self.request(
                'initialize',
                {
                    'clientInfo': {
                        'name': 'cc8_bumblebee_voice',
                        'title': 'CC8 Bumblebee',
                        'version': RELEASE.backend_version,
                    },
                    'capabilities': {'experimentalApi': True},
                },
                timeout=remaining(),
            )
            remaining()
            self._send({'method': 'initialized', 'params': {}}, deadline=deadline)
            remaining()
        except Exception:
            # A partial thread/initialize failure still owns its guardian/pipes.
            # Preserve the startup cause if releasing those resources also fails.
            try:
                self.close()
            except Exception:
                pass
            raise
        with self.lifecycle_lock:
            self.initialized = not self.failed and not self.guardian_ending.is_set()

    def _send(self, value, *, deadline=None):
        if deadline is None:
            deadline = deadline_after(15)
        wire = (json.dumps(value, ensure_ascii=False) + '\n').encode()
        if len(wire) > self.MAX_LINE:
            raise DotError('Codex request exceeds size limit')
        process = self.process
        if process is None or self.failed:
            raise DotError('Codex is disconnected', kind='transport_or_io')
        try:
            self.writer.send(
                process.stdin, wire, deadline, lambda: self.process is process and not self.failed
            )
        except DotError:
            if self.writer.unusable:
                self._disconnect()
            raise

    def notify(self, method, params):
        self._send({'method': method, 'params': params})

    def request(self, method, params, timeout=15):
        deadline = deadline_after(timeout)
        with self.lock:
            self.sequence += 1
            ident = self.sequence
            waiter = queue.Queue(maxsize=1)
            self.pending[ident] = waiter
        try:
            self._send({'id': ident, 'method': method, 'params': params}, deadline=deadline)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                result = waiter.get(timeout=remaining)
                # A scheduler delay can make Queue.get return a newly queued
                # reply after its timeout elapsed. Keep the original RPC budget.
                if time.monotonic() >= deadline:
                    raise queue.Empty
            except queue.Empty:
                if method == 'turn/start':
                    raise UnconfirmedTurnStart(kind='timeout') from None
                raise DotError('Codex request timed out', kind='timeout') from None
            if isinstance(result, Exception):
                if (
                    method == 'turn/start'
                    and isinstance(result, DotError)
                    and result.kind == 'transport_or_io'
                ):
                    raise UnconfirmedTurnStart(kind=result.kind) from None
                raise result
            if result.get('error'):
                raise DotError('Codex RPC request failed')
            return result.get('result') or {}
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def _server_request(self, message):
        # 服务端工具请求不等于用户授权，需要审批的动作不能在这里自动批准。
        method = message.get('method', '')
        if method == 'item/tool/call':

            def execute():
                try:
                    if not self.tool_handler:
                        raise DotError('Voice tools unavailable')
                    value = self.tool_handler(message.get('params') or {})
                    result = {
                        'success': bool(value.get('ok')),
                        'contentItems': [
                            {'type': 'inputText', 'text': json.dumps(value, ensure_ascii=False)}
                        ],
                    }
                except Exception as error:
                    result = {
                        'success': False,
                        'contentItems': [
                            {
                                'type': 'inputText',
                                'text': json.dumps({'ok': False, 'error': type(error).__name__}),
                            }
                        ],
                    }
                try:
                    self._send({'id': message['id'], 'result': result})
                except DotError:
                    pass

            threading.Thread(target=execute, daemon=True).start()
            return
        if method == 'item/permissions/requestApproval':
            self._record_approval(message)
            result = {'permissions': {}, 'scope': 'turn'}
        elif method.endswith('/requestApproval'):
            self._record_approval(message)
            result = {'decision': 'decline'}
        elif method == 'item/tool/requestUserInput':
            result = {'answers': {}}
        else:
            self._send(
                {
                    'id': message['id'],
                    'error': {'code': -32601, 'message': 'Unsupported by this voice client'},
                }
            )
            return
        self._send({'id': message['id'], 'result': result})

    def _record_approval(self, message):
        # Keep only bounded IDs, not commands/paths or permission contents.
        # The reader may see a request before turn/start returns its turn ID.
        self.approval_seen.set()
        params = message.get('params')
        if not isinstance(params, dict):
            return
        scope = (params.get('threadId'), params.get('turnId'))
        if not all(isinstance(value, str) and 0 < len(value) <= 4096 for value in scope):
            return
        with self.approval_lock:
            if scope not in self.approval_terminals and scope not in self.approval_scopes:
                self.approval_scopes.append(scope)

    def _finish_approval(self, message):
        params = message.get('params')
        turn = params.get('turn') if isinstance(params, dict) else None
        if not isinstance(turn, dict):
            return
        scope = (params.get('threadId'), turn.get('id'))
        if not all(isinstance(value, str) and 0 < len(value) <= 4096 for value in scope):
            return
        # Freeze before receipt listeners or the speech consumer run. A later
        # request cannot retroactively become the cause of a terminal result.
        with self.approval_lock:
            if scope not in self.approval_terminals:
                self.approval_terminals.append(scope)

    def approval_required(self, thread_id, turn_id):
        with self.approval_lock:
            return (thread_id, turn_id) in self.approval_scopes

    def _read(self, process=None):
        process = self.process if process is None else process
        try:
            while True:
                line = process.stdout.readline(self.MAX_LINE + 1)
                if not line:
                    break
                if len(line) > self.MAX_LINE:
                    raise DotError('Codex message exceeds size limit')
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise DotError('Codex message must be an object')
                if 'id' in message and 'method' in message:
                    self._server_request(message)
                elif 'id' in message:
                    with self.lock:
                        waiter = self.pending.get(message['id'])
                    if waiter:
                        waiter.put_nowait(message)
                else:
                    method = message.get('method')
                    params = message.get('params') or {}
                    relevant = method in ('turn/completed', 'item/agentMessage/delta', 'error') or (
                        method in ('item/started', 'item/completed')
                        and (params.get('item') or {}).get('type')
                        in (
                            'agentMessage',
                            'webSearch',
                            'commandExecution',
                            'fileChange',
                            'mcpToolCall',
                            'dynamicToolCall',
                        )
                    )
                    if relevant:
                        self._receive(message)
        except (ValueError, TypeError, AttributeError, OSError, queue.Full, DotError) as error:
            self.failure_reason = (
                str(error) if isinstance(error, DotError) else type(error).__name__
            )
        finally:
            try:
                self._disconnect()
            finally:
                # The reader closes only after all admitted receipts have drained.
                process.stdout.close()

    def _disconnect(self):
        self.failed = True
        with self.lock:
            waiters = list(self.pending.values())
        for waiter in waiters:
            try:
                waiter.put_nowait(DotError('Codex disconnected', kind='transport_or_io'))
            except queue.Full:
                pass

    def close(self):
        with self.lifecycle_lock:
            self._disconnect()
            self.initialized = False
            guardian, self.guardian = self.guardian, None
            confirmed = close_guardian(self.process, guardian)
            if not confirmed:
                self.failure_reason = 'guardian_cleanup_unconfirmed'
            elif self.process is not None:
                # A started reader owns its pipe until EOF, including callbacks
                # already admitted. Never close it from this foreign thread.
                pipes = [self.process.stdin]
                for reader, stream in (
                    (self.stdout_reader, self.process.stdout),
                    (self.stderr_reader, self.process.stderr),
                ):
                    if reader is None or reader.ident is None:
                        pipes.append(stream)
                # Disconnect makes an active nonblocking writer yield its lock.
                with self.writer.lock:
                    cleanup_all(*(stream.close for stream in pipes))
            return confirmed

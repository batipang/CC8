"""Failure-path task completion and stop fan-out through real owners."""

import io
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from brain_router import BrainRouter
from bridge import Bridge
from dot_client import DotBrain, CodexRpc, Turn
from failure_feedback import feedback
from operation_failure import OperationFailure
from reply_types import Reply
import app_runtime

SECRET = 'private-error-detail-must-not-be-published'


class CleanupCompletionTests(unittest.TestCase):
    def response(self, *, receipt=False, primary=None, cancel=False):
        closed = []

        class Brain:
            last_metrics = {}

            def cancel(self):
                pass

            def clauses(self, text, history, current, update):
                try:
                    if receipt:
                        update('dot_result', {'text': 'Work complete.', 'truncated': False})
                    yield 'Answer.'
                    self.last_metrics = {'completed': True}
                finally:
                    closed.append(1)
                    raise RuntimeError(SECRET)

        with patch('threading.Thread.start'):
            b = Bridge(None, Brain())
        events, logs = [], []
        b.session.active = b.session.awake = True
        b.session.connection = object()
        b.event = lambda name, **data: events.append((name, data))
        b.log = lambda name, **data: logs.append((name, data))
        b.save_status = lambda: None

        def speak(*args):
            if primary:
                raise primary
            if cancel:
                b.cancel('test')
                return False
            return True

        b.speak = speak
        with patch.object(
            b.response.queue, 'get', side_effect=[Reply(b.generation, 'Work'), SystemExit]
        ):
            with self.assertRaises(SystemExit):
                b.response.work()
        return b, events, logs, closed

    def test_cleanup_error_cannot_leave_task_running_or_replace_primary_failure(self):
        primary = OperationFailure(SECRET, stage='tts', kind='rate_limit')
        b, events, logs, closed = self.response(primary=primary)
        self.assertEqual(closed, [1])
        task = b.workspace.tasks[-1]
        self.assertEqual(task['status'], 'failed')
        row = b.diagnostics.report()['requests'][-1]
        self.assertEqual(row['outcome'], 'failed')
        self.assertEqual(row['error'], {'stage': 'tts', 'kind': 'rate_limit'})
        self.assertIn(feedback('tts', 'rate_limit').message, task['result'])
        self.assertNotIn(SECRET, str((events, logs, row)))

    def test_cancelled_answer_still_finishes_trace_when_provider_close_fails(self):
        b, events, logs, _ = self.response(cancel=True)
        self.assertEqual(b.workspace.tasks[-1]['status'], 'cancelled')
        row = b.diagnostics.report()['requests'][-1]
        self.assertEqual(row['outcome'], 'cancelled')
        self.assertFalse(b.response.busy)
        self.assertFalse(
            any(name == 'status' and fields.get('phase') == 'error' for name, fields in events)
        )
        self.assertNotIn(SECRET, str(logs))

    def test_already_completed_result_survives_both_playback_and_close_failure(self):
        primary = TimeoutError(SECRET)
        b, events, logs, _ = self.response(receipt=True, primary=primary)
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        self.assertEqual(b.workspace.tasks[-1]['result'], 'Work complete.')
        row = b.diagnostics.report()['requests'][-1]
        self.assertEqual(row['task_outcome'], 'completed')
        self.assertEqual(row['outcome'], 'failed')
        self.assertEqual(row['error']['kind'], 'timeout')
        self.assertNotIn(SECRET, str((events, logs)))

    def test_router_cancellation_attempts_both_owners_before_raising_original(self):
        for action in ('cancel', 'close'):
            first = OSError(SECRET)
            mini, dot = Mock(), Mock()
            mini.cancel.side_effect = first
            dot.cancel.side_effect = ValueError('secondary')
            router = BrainRouter(mini, dot)
            with self.subTest(action=action), self.assertRaises(OSError) as caught:
                getattr(router, action)()
            self.assertIs(caught.exception, first)
            self.assertTrue(dot.cancel.called)
            if action == 'close':
                dot.close.assert_called_once_with()

    def test_dot_close_still_closes_rpc_if_interrupt_cannot_start(self):
        rpc = Mock()
        dot = DotBrain(rpc)
        original = RuntimeError(SECRET)
        dot.cancel = Mock(side_effect=original)
        with self.assertRaises(RuntimeError) as caught:
            dot.close()
        self.assertIs(caught.exception, original)
        rpc.close.assert_called_once_with()

    def registered_stop(self):
        b, hub, announcer, speech = Mock(), Mock(), Mock(), Mock()
        b.speech = speech
        b.session.active = False
        handlers = {}

        def stop_event(**kwargs):
            handlers[signal.SIGTERM]()
            return 'down', None, None

        def run(get_event=None):
            hub.events.get.side_effect = get_event or stop_event
            with tempfile.TemporaryDirectory() as folder:
                dependencies = app_runtime.BackendDependencies(
                    private=Path(folder),
                    description='test',
                    load_config=Mock(
                        return_value=SimpleNamespace(
                            brain='dot',
                            tls_port=18766,
                            wake_window_seconds=60,
                            chinese_wake_verifier='off',
                        )
                    ),
                    direct_speech=Mock(return_value=speech),
                    local_speech=Mock(),
                    fast_stop=Mock(),
                    wake_detector=Mock(),
                    brain_router=Mock(),
                    minimax_brain=Mock(),
                    bridge=Mock(return_value=b),
                    workspace=Mock(),
                    transport_hub=Mock(return_value=hub),
                    pairing=Mock(),
                    connect_device=Mock(),
                    usb_present=Mock(),
                    announcement_server=Mock(return_value=announcer),
                )
                with (
                    patch.object(sys, 'argv', ['bridge.py', '--no-fast-stop']),
                    patch('app_runtime.threading.Thread.start'),
                    patch(
                        'app_runtime.signal.signal',
                        side_effect=lambda name, handler: handlers.update({name: handler}),
                    ),
                    patch('sys.stdout', new_callable=io.StringIO),
                ):
                    app_runtime.run_backend(dependencies)

        return run, b, hub, announcer, speech

    def test_rpc_guardian_exits_and_lifetime_socket_closes_if_cancel_raises(self):
        server = (
            'import json,sys\n'
            'for line in sys.stdin:\n'
            ' request=json.loads(line)\n'
            ' if "id" in request:\n'
            '  print(json.dumps({"id":request["id"],"result":{}}),flush=True)\n'
        )
        rpc = CodexRpc(command=[sys.executable, '-u', '-c', server])
        try:
            rpc.start()
            process, lifetime = rpc.process, rpc.guardian
            self.assertIsNone(process.poll())
            dot = DotBrain(rpc)
            turn = Turn(thread_id='fixture', ident='fixture')
            dot.current = turn
            failure = RuntimeError(SECRET)
            with (
                patch('dot_client.threading.Thread.start', side_effect=failure),
                self.assertRaises(RuntimeError) as caught,
            ):
                dot.close()
            self.assertIs(caught.exception, failure)
            self.assertTrue(turn.stopped.is_set())
            self.assertIsNotNone(process.poll())
            self.assertEqual(lifetime.fileno(), -1)
        finally:
            rpc.close()
            if rpc.process is not None:
                for stream in (rpc.process.stdin, rpc.process.stdout, rpc.process.stderr):
                    stream.close()

    def test_registered_shutdown_does_not_skip_later_resources_after_one_failure(self):
        for stage in (
            'deactivate',
            'playout',
            'invalidate',
            'cancel',
            'ipc',
            'hub',
            'brain',
            'speech',
            'channel_shutdown',
            'channel_close',
        ):
            handler, b, hub, announcer, speech = self.registered_stop()
            source = {
                'deactivate': b.session.deactivate,
                'playout': b.playback.stop_output,
                'invalidate': b.announcements.invalidate,
                'cancel': b.cancel,
                'ipc': announcer.close,
                'hub': hub.close,
                'brain': b.brain.close,
                'speech': speech.cancel,
                'channel_shutdown': b.session.connection.shutdown,
                'channel_close': b.session.connection.close,
            }[stage]
            original = RuntimeError(SECRET)
            source.side_effect = original
            with self.subTest(stage=stage), self.assertRaises(RuntimeError) as caught:
                handler()
            self.assertIs(caught.exception, original)
            b.session.deactivate.assert_called_once()
            b.playback.stop_output.assert_called_once()
            b.announcements.invalidate.assert_called_once()
            b.cancel.assert_called_once_with('shutdown')
            announcer.close.assert_called_once()
            hub.close.assert_called_once()
            b.brain.close.assert_called_once()
            speech.cancel.assert_called_once()
            b.session.connection.shutdown.assert_called_once()
            b.session.connection.close.assert_called_once()

    def test_expected_cancel_and_channel_io_errors_still_finish_shutdown(self):
        for stage in ('cancel', 'channel_shutdown', 'channel_close'):
            handler, b, hub, announcer, speech = self.registered_stop()
            source = {
                'cancel': b.cancel,
                'channel_shutdown': b.session.connection.shutdown,
                'channel_close': b.session.connection.close,
            }[stage]
            source.side_effect = OSError(SECRET)
            with self.subTest(stage=stage):
                handler()
                announcer.close.assert_called_once()
                hub.close.assert_called_once()
                b.brain.close.assert_called_once()
                speech.cancel.assert_called_once()
                b.session.connection.close.assert_called_once()

    def test_partial_startup_failure_releases_resources_that_were_constructed(self):
        for stage in ('hub', 'ipc'):
            run, b, hub, announcer, speech = self.registered_stop()
            source = hub.start if stage == 'hub' else announcer.start
            primary = OSError(SECRET)
            source.side_effect = primary
            with self.subTest(stage=stage), self.assertRaises(OSError) as caught:
                run()
            self.assertIs(caught.exception, primary)
            hub.close.assert_called_once()
            b.brain.close.assert_called_once()
            speech.cancel.assert_called_once()
            b.session.unbind.assert_called_once()
            self.assertEqual(announcer.close.call_count, int(stage == 'ipc'))

    def test_loop_failure_keeps_primary_cause_after_secondary_cleanup_failure(self):
        run, b, hub, announcer, speech = self.registered_stop()
        primary = KeyError(SECRET)
        hub.close.side_effect = RuntimeError('secondary')
        with self.assertRaises(KeyError) as caught:
            run(Mock(side_effect=primary))
        self.assertIs(caught.exception, primary)
        announcer.close.assert_called_once()
        b.brain.close.assert_called_once()
        speech.cancel.assert_called_once()
        b.session.unbind.assert_called_once()
        self.assertEqual(b.save_status.call_count, 2)
        self.assertNotIn(SECRET, str(b.log.call_args_list))


if __name__ == '__main__':
    unittest.main()

"""Injected failures exercise real owners, without providers, devices or capture."""

from test_memory_fixture import isolated_memory
import json
import threading
import unittest
from unittest.mock import patch
import numpy as np
from brain_router import BrainRouter
from bridge import Bridge
from dot_client import DotApprovalRequired, DotError, CodexRpc
from failure_feedback import feedback
from minimax_client import MiniMaxError
from operation_failure import KINDS, STAGES, OperationFailure
from reply_types import Reply

SECRET = 'SECRET-credential-transcript-provider-url'


class Brain:
    def __init__(self, error=None, receipt=False):
        self.error = error
        self.receipt = receipt
        self.calls = 0
        self.last_metrics = {}

    def cancel(self):
        pass

    def clauses(self, text, history, current, on_web, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        if self.receipt:
            on_web('dot_result', {'text': '真实任务完成', 'truncated': False})
        self.last_metrics = {'completed': True}
        yield '已完成。'


class FailureFeedbackTests(unittest.TestCase):
    def session(self, speech=None, brain=None):
        with patch('threading.Thread.start'):
            bridge = Bridge(speech, brain, preference_memory=isolated_memory())
        bridge.session.active = True
        bridge.session.awake = True
        bridge.session.connection = object()
        bridge.save_status = lambda: None
        bridge.events = []
        bridge.logs = []
        bridge.event = lambda name, **fields: bridge.events.append((name, fields))
        bridge.log = lambda name, **fields: bridge.logs.append((name, fields))
        return bridge

    def run_one(self, worker, job):
        # Run the actual worker loop until it asks for another job; no daemon leak.
        with patch.object(worker.queue, 'get', side_effect=[job, SystemExit]):
            with self.assertRaises(SystemExit):
                worker.work()

    def guidance(self, bridge):
        return [
            fields['message']
            for name, fields in bridge.events
            if name == 'status' and fields['phase'] == 'error'
        ]

    def test_fixed_guidance_covers_categories_without_fake_pending_approval(self):
        for stage in STAGES:
            for kind in KINDS:
                result = feedback(stage, kind)
                self.assertLess(len(result.message), 180)
                self.assertEqual((result.stage, result.kind), (stage, kind))
                if kind == 'approval_declined':
                    self.assertIn('当前没有待批准', result.message)
                    self.assertIn('本次未执行', result.message)
        for invalid in [('SECRET', 'timeout'), ('asr', 'SECRET')]:
            with self.assertRaises(ValueError):
                feedback(*invalid)

    def test_brain_failure_matrix_is_terminal_content_free_and_never_retried(self):
        errors = [
            (DotError(SECRET, kind='timeout'), 'timeout'),
            (MiniMaxError(SECRET, http_status=429), 'rate_limit'),
            (MiniMaxError(SECRET, http_status=402), 'quota'),
            (MiniMaxError(SECRET, http_status=401), 'credential_or_permission'),
            (DotError(SECRET, kind='transport_or_io'), 'transport_or_io'),
            (DotApprovalRequired(SECRET), 'approval_declined'),
        ]
        for error, kind in errors:
            for _ in range(3):
                with self.subTest(kind=kind):
                    mini, dot = Brain(), Brain(error)
                    router = BrainRouter(mini, dot)
                    b = self.session(brain=router)
                    b.speak = lambda *a: self.fail('An error is not a normal answer clause')
                    self.run_one(b.response, Reply(b.generation, '测试任务'))
                    self.assertEqual(dot.calls, 1)
                    self.assertEqual(mini.calls, 0)
                    self.assertFalse(b.response.busy)
                    self.assertEqual(b.workspace.tasks[-1]['status'], 'failed')
                    row = b.diagnostics.report()['requests'][-1]
                    self.assertEqual(row['outcome'], 'failed')
                    self.assertEqual(row['error'], {'stage': 'brain', 'kind': kind})
                    self.assertTrue(self.guidance(b))
                    self.assertIn('电脑端', self.guidance(b)[-1])
                    self.assertEqual(list(b.response.history), [])
                    self.assertIn('电脑端', b.workspace.tasks[-1]['result'])
                    self.assertNotIn(SECRET, json.dumps([b.events, b.logs, b.diagnostics.report()]))

    def test_asr_failure_matrix_does_not_dispatch_a_task_or_retry(self):
        errors = [
            (TimeoutError(SECRET), 'timeout'),
            (MiniMaxError(SECRET, http_status=429), 'rate_limit'),
            (MiniMaxError(SECRET, http_status=402), 'quota'),
            (MiniMaxError(SECRET, http_status=403), 'credential_or_permission'),
            (ConnectionError(SECRET), 'transport_or_io'),
        ]
        for error, kind in errors:
            for _ in range(3):
                with self.subTest(kind=kind):
                    calls = []

                    class Speech:
                        def recognize(self, pcm):
                            calls.append(1)
                            raise error

                    b = self.session(Speech(), Brain())
                    key = b.diagnostics.begin('voice')
                    self.run_one(
                        b.recognition, (b.generation, np.ones(16000), False, [], None, None, key)
                    )
                    self.assertEqual(calls, [1])
                    self.assertTrue(b.response.queue.empty())
                    self.assertFalse(b.recognition.busy)
                    self.assertEqual(b.workspace.tasks, [])
                    row = b.diagnostics.report()['requests'][-1]
                    self.assertEqual(row['outcome'], 'failed')
                    self.assertEqual(row['error'], {'stage': 'asr', 'kind': kind})
                    self.assertIn('这句话未交给助理', self.guidance(b)[-1])
                    self.assertNotIn(SECRET, json.dumps([b.events, b.logs, row]))

    def test_tts_failure_matrix_keeps_completed_task_and_does_not_repeat_it(self):
        errors = [
            (TimeoutError(SECRET), 'timeout'),
            (MiniMaxError(SECRET, http_status=429), 'rate_limit'),
            (MiniMaxError(SECRET, http_status=402), 'quota'),
            (MiniMaxError(SECRET, http_status=401), 'credential_or_permission'),
            (ConnectionError(SECRET), 'transport_or_io'),
        ]
        for error, kind in errors:
            for _ in range(3):
                with self.subTest(kind=kind):
                    calls = []

                    class Speech:
                        def synthesize(self, text):
                            calls.append(1)
                            raise error

                    dot = Brain(receipt=True)
                    b = self.session(Speech(), BrainRouter(Brain(), dot))
                    self.run_one(b.response, Reply(b.generation, '测试任务'))
                    self.assertEqual(dot.calls, 1)
                    self.assertEqual(calls, [1])
                    self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
                    row = b.diagnostics.report()['requests'][-1]
                    self.assertEqual(row['task_outcome'], 'completed')
                    self.assertEqual(row['outcome'], 'failed')
                    self.assertEqual(row['error'], {'stage': 'tts', 'kind': kind})
                    self.assertIn('不要重复交办', self.guidance(b)[-1])
                    self.assertNotIn(SECRET, json.dumps([b.events, b.logs, row]))

    def test_late_tts_failure_keeps_its_stage_after_pcm_and_cancels_output(self):
        class Speech:
            def stream(self, text, current):
                yield np.ones(3200, dtype=np.int16)
                raise TimeoutError(SECRET)

        b = self.session(Speech(), BrainRouter(Brain(), Brain(receipt=True)))
        b.send = lambda packet: None
        generation = b.generation
        self.run_one(b.response, Reply(generation, '测试任务'))
        row = b.diagnostics.report()['requests'][-1]
        self.assertIn('first_pcm', row['stages'])
        self.assertEqual(row['error'], {'stage': 'tts', 'kind': 'timeout'})
        self.assertGreater(b.generation, generation)
        self.assertFalse(b.playback.playing)
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')
        self.assertTrue(self.guidance(b))

    def test_dot_rpc_timeout_is_classified_without_message_matching_or_resend(self):
        rpc = CodexRpc.__new__(CodexRpc)
        rpc.lock = threading.Lock()
        rpc.sequence = 0
        rpc.pending = {}
        sent = []
        rpc._send = lambda value, **_: sent.append(value)
        with self.assertRaises(DotError) as caught:
            rpc.request('readOnlyFixture', {}, timeout=0.001)
        self.assertEqual(caught.exception.kind, 'timeout')
        self.assertEqual(caught.exception.stage, 'brain')
        self.assertEqual(len(sent), 1)
        self.assertEqual(rpc.pending, {})

    def test_device_playback_error_is_visible_and_never_retries_the_task(self):
        b = self.session()
        b.diagnostics.begin('text', b.generation)
        b.on_event(
            {'event': 'playback_error', 'generation': b.generation, 'error_type': 'FixtureError'}
        )
        self.assertIn('设备播放', self.guidance(b)[-1])
        self.assertTrue(b.response.queue.empty())

    def test_device_playback_error_log_accepts_only_known_exception_names(self):
        for error_type, expected in (
            ('IOException', 'IOException'),
            (SECRET, 'Unknown'),
            ({'message': SECRET}, 'Unknown'),
            (True, 'Unknown'),
            (None, 'Unknown'),
        ):
            with self.subTest(expected=expected, kind=type(error_type).__name__):
                b = self.session()
                b.diagnostics.begin('text', b.generation)
                b.on_event(
                    {
                        'event': 'playback_error',
                        'generation': b.generation,
                        'error_type': error_type,
                    }
                )
                events = [fields for name, fields in b.logs if name == 'device_playback_error']
                self.assertEqual(events, [{'error_type': expected}])
                self.assertNotIn(SECRET, json.dumps([b.logs, b.events, b.diagnostics.report()]))
                self.assertIn('设备播放', self.guidance(b)[-1])

    def test_missing_play_done_is_a_failure_with_bounded_wait_not_silent_success(self):
        b = self.session()
        generation = b.generation
        b.diagnostics.begin('announcement', generation)
        b.send = lambda packet: None
        now = [0.0]

        def wait(_):
            now[0] += 5

        with (
            patch('playback_controller.time.monotonic', side_effect=lambda: now[0]),
            patch.object(b.playback.done, 'wait', side_effect=wait),
        ):
            self.assertFalse(b.play(np.ones(3200, dtype=np.int16), '测试', generation))
        row = b.diagnostics.report()['requests'][-1]
        self.assertEqual(row['error'], {'stage': 'playback', 'kind': 'timeout'})
        self.assertIn('设备播放', self.guidance(b)[-1])
        self.assertFalse(b.playback.playing)

    def test_stale_response_error_does_not_overwrite_a_new_generation(self):
        b = self.session()
        old = b.generation

        def dispatch(job):
            b.session.generation += 1
            raise DotError(SECRET)

        b.respond_job = dispatch
        self.run_one(b.response, Reply(old, '旧任务'))
        self.assertEqual(self.guidance(b), [])
        self.assertEqual(b.generation, old + 1)

    def test_operation_failure_rejects_unknown_categories(self):
        with self.assertRaises(ValueError):
            OperationFailure(SECRET, stage='brain', kind='unverified_vendor_code')


class TransportClassificationTests(unittest.TestCase):
    def test_real_minimax_transport_preserves_timeout_and_disconnect_categories(self):
        import requests
        from diagnostics import error_kind
        from minimax_client import Transport

        for exception, wanted in [
            (requests.ReadTimeout(SECRET), 'timeout'),
            (requests.ConnectionError(SECRET), 'transport_or_io'),
        ]:
            for operation in ('transcribe', 'post_json', 'events'):
                for _ in range(3):
                    with self.subTest(operation=operation, kind=wanted):

                        class Session:
                            calls = 0
                            closed = False

                            def post(self, *args, **kwargs):
                                self.calls += 1
                                raise exception

                            def close(self):
                                self.closed = True

                        session = Session()
                        transport = Transport(key='sk-cp-fixture', session_factory=lambda: session)
                        with self.assertRaises(MiniMaxError) as caught:
                            if operation == 'transcribe':
                                transport.transcribe(b'fixture')
                            elif operation == 'post_json':
                                transport.post_json('/fixture', {})
                            else:
                                list(transport.events('/fixture', {}))
                        self.assertEqual(error_kind(caught.exception), wanted)
                        self.assertNotIn(SECRET, str(caught.exception))
                        self.assertTrue(session.closed)
                        self.assertEqual(session.calls, 1)
                        self.assertEqual(transport.slots, set())


if __name__ == '__main__':
    unittest.main()

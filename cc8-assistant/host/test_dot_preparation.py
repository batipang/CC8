"""Connection-only preparation on the actual serial voice response path."""

import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from announce import Announcement
from brain_router import BrainRouter
from dot_client import DotBrain, DotError
from reply_types import BrainPreparation, Reply
import test_failure_feedback as fixture
from test_dot_client import FakeRpc
from test_dot_timing import TimedRpc


class PreparationTests(unittest.TestCase):
    def test_initial_context_only_then_first_real_turn_reuses_it(self):
        now = [0.0]
        rpc = TimedRpc(now)
        brain = DotBrain(rpc)
        self.assertEqual(rpc.calls, [])
        brain.clock = lambda: now[0]
        previous = brain.last_metrics
        self.assertTrue(brain.prepare())
        self.assertIs(brain.last_metrics, previous)
        self.assertIsNone(brain.current)
        self.assertIsNone(rpc.tool_handler)
        self.assertEqual([m for m, _ in rpc.calls], ['thread/start'])
        thread = rpc.calls[0][1]
        self.assertTrue(thread['ephemeral'])
        for forbidden in ('model', 'effort', 'approvalPolicy', 'sandbox', 'sandboxPolicy'):
            self.assertNotIn(forbidden, thread)
        self.assertTrue(thread['dynamicTools'])
        stream = brain.clauses('固定公开测试', [])
        next(stream)
        rpc.complete()
        list(stream)
        self.assertEqual([m for m, _ in rpc.calls], ['thread/start', 'turn/start'])
        self.assertTrue(brain.last_metrics['rpc_reused'])
        self.assertTrue(brain.last_metrics['context_reused'])

    def test_prepare_failure_has_short_context_budget_no_retry_or_task(self):
        class FailedRpc(FakeRpc):
            def request(self, method, params, timeout=15):
                self.calls.append((method, timeout))
                raise DotError('SECRET fixed failure', kind='timeout')

        rpc = FailedRpc()
        brain = DotBrain(rpc)
        original = brain.last_metrics
        with self.assertRaises(DotError):
            brain.prepare()
        self.assertEqual(rpc.calls, [('thread/start', 10)])
        self.assertTrue(rpc.closed)
        self.assertFalse(brain.preparing)
        self.assertIsNone(brain.thread_id)
        self.assertIsNone(brain.current)
        self.assertIs(brain.last_metrics, original)

    def test_inflight_context_selection_rejected_and_cancelled_readiness_not_published(self):
        entered = threading.Event()
        release = threading.Event()
        current = threading.Event()
        current.set()

        class HeldRpc(FakeRpc):
            def request(self, method, params, timeout=15):
                if method == 'thread/start':
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError('test release missing')
                return super().request(method, params, timeout)

        rpc = HeldRpc()
        brain = DotBrain(rpc)
        results = []
        worker = threading.Thread(target=lambda: results.append(brain.prepare(current.is_set)))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(DotError):
                brain.set_handoff(SimpleNamespace(cwd='test'))
            current.clear()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        self.assertEqual([m for m, _ in rpc.calls], ['thread/start'])
        self.assertIsNone(brain.handoff)
        self.assertFalse(brain.preparing)

    def test_existing_or_selected_context_and_minimax_do_not_start_preparation(self):
        rpc = FakeRpc()
        brain = DotBrain(rpc)
        brain.thread_id = 'already-existing'
        self.assertTrue(brain.prepare())
        self.assertEqual(rpc.calls, [])
        brain.set_handoff(SimpleNamespace(cwd='test'))
        self.assertFalse(brain.prepare())
        self.assertEqual(rpc.calls, [])
        router = BrainRouter(fixture.Brain(), DotBrain(FakeRpc()), selected='minimax')
        self.assertFalse(router.prepare())
        self.assertEqual(router.dot.rpc.calls, [])

    def test_serial_worker_prepares_then_executes_only_the_actual_request(self):
        case = fixture.FailureFeedbackTests()
        rpc = FakeRpc()
        bridge = case.session(brain=BrainRouter(fixture.Brain(), DotBrain(rpc)))
        bridge.speak = lambda *args: True
        generation = bridge.generation
        with patch.object(
            bridge.response.queue,
            'get',
            side_effect=[BrainPreparation(generation), Reply(generation, '固定测试'), SystemExit],
        ):
            with self.assertRaises(SystemExit):
                bridge.response.work()
        self.assertEqual([m for m, _ in rpc.calls], ['thread/start', 'turn/start'])
        self.assertEqual(len(bridge.workspace.tasks), 1)
        self.assertEqual(len(bridge.diagnostics.report()['requests']), 1)
        self.assertFalse(bridge.response.busy)
        self.assertEqual(bridge.workspace.tasks[0]['status'], 'completed')

    def test_preparation_queue_full_preserves_real_jobs_and_stale_work_is_skipped(self):
        case = fixture.FailureFeedbackTests()
        rpc = FakeRpc()
        bridge = case.session(brain=DotBrain(rpc))
        first = Reply(bridge.generation, 'first')
        second = Announcement(bridge.generation, 'second')
        bridge.response.offer(first)
        bridge.response.offer(second)
        bridge.response.prepare(bridge.generation)
        self.assertIs(bridge.response.queue.get_nowait(), first)
        self.assertIs(bridge.response.queue.get_nowait(), second)
        with self.assertRaises(queue.Empty):
            bridge.response.queue.get_nowait()
        bridge.respond_job(BrainPreparation(bridge.generation - 1))
        bridge.session.active = False
        bridge.respond_job(BrainPreparation(bridge.generation))
        self.assertEqual(rpc.calls, [])

    def test_optional_failure_does_not_create_task_or_prevent_next_real_request(self):
        class Brain(fixture.Brain):
            def prepare(self, current):
                raise DotError('SECRET test failure', kind='timeout')

        case = fixture.FailureFeedbackTests()
        brain = Brain(receipt=True)
        bridge = case.session(brain=brain)
        bridge.speak = lambda *args: True
        bridge.respond_job(BrainPreparation(bridge.generation))
        self.assertEqual(bridge.workspace.tasks, [])
        self.assertEqual(bridge.diagnostics.report()['requests'], [])
        self.assertEqual(bridge.events, [])
        self.assertEqual(
            bridge.logs,
            [('brain_prepare_failed', {'error_type': 'DotError'})],
        )
        case.run_one(bridge.response, Reply(bridge.generation, '固定测试'))
        self.assertEqual(brain.calls, 1)
        self.assertEqual(bridge.workspace.tasks[-1]['status'], 'completed')

    def test_preparation_log_failure_cannot_kill_worker_or_invent_answer_failure(self):
        class Brain(fixture.Brain):
            def prepare(self, current):
                return True

        case = fixture.FailureFeedbackTests()
        bridge = case.session(brain=Brain(receipt=True))
        bridge.speak = lambda *args: True

        def broken_log(name, **fields):
            if name.startswith('brain_prepare'):
                raise OSError('SECRET disk failure')

        bridge.log = broken_log
        generation = bridge.generation
        with patch.object(
            bridge.response.queue,
            'get',
            side_effect=[BrainPreparation(generation), Reply(generation, '固定测试'), SystemExit],
        ):
            with self.assertRaises(SystemExit):
                bridge.response.work()
        self.assertEqual(len(bridge.workspace.tasks), 1)
        self.assertEqual(bridge.workspace.tasks[0]['status'], 'completed')
        self.assertEqual(case.guidance(bridge), [])

    def test_only_new_explicit_capture_epoch_enqueues_preparation_and_stop_invalidates_it(self):
        case = fixture.FailureFeedbackTests()
        rpc = FakeRpc()
        bridge = case.session(brain=DotBrain(rpc))
        self.assertTrue(bridge.response.queue.empty())
        bridge.session.active = False
        with patch('threading.Thread.start'), patch.object(bridge.capture.echo, 'open'):
            bridge.session.start_capture({'epoch': 7})
            bridge.session.start_capture({'epoch': 7})
        job = bridge.response.queue.get_nowait()
        self.assertIsInstance(job, BrainPreparation)
        self.assertTrue(bridge.response.queue.empty())
        self.assertTrue(bridge.calibration.active)
        bridge.stop()
        bridge.respond_job(job)
        self.assertFalse(bridge.session.active)
        self.assertEqual(rpc.calls, [])


if __name__ == '__main__':
    unittest.main()

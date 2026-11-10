"""Serial operations with real producer receipts, cancellation and no automatic replay."""

from copy import deepcopy
import unittest

from workflow_definition import builtins, fingerprint
from workflow_run import WorkflowRun, StepOutput, WorkflowUnconfirmed


def flow(count=2):
    value = builtins()[0]
    value['steps'] = [
        dict(id=str(i) * 32, kind='assistant', title='步骤' + str(i), text='任务' + str(i))
        for i in range(1, count + 1)
    ]
    return value


def receipt(text='实际结果', kind='assistant_turn'):
    return StepOutput(kind, 'operation-1', text, True)


class RunTests(unittest.TestCase):
    def test_ordered_success_with_per_step_receipts_and_private_results(self):
        run = WorkflowRun(flow())
        calls, published = [], []

        def execute(step, current, deadline, accepted):
            self.assertTrue(current())
            calls.append(step['id'])
            return receipt(step['text'])

        result = run.run(execute, current=lambda: True, publish=published.append)
        self.assertEqual(calls, [s['id'] for s in flow()['steps']])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual([s['result']['text'] for s in result['steps']], ['任务1', '任务2'])
        self.assertFalse(result['independently_verified'])
        self.assertTrue(all('result' not in row for p in published for row in p['steps']))
        with self.assertRaises(ValueError):
            run.run(execute, current=lambda: True)

    def test_failure_stops_remaining_steps_without_replaying_completed_operation(self):
        run = WorkflowRun(flow(3))
        calls = []

        def execute(step, *args):
            calls.append(step['id'])
            if len(calls) == 2:
                raise OSError('lost reply; execution may have happened')
            return receipt()

        result = run.run(execute, current=lambda: True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(
            [s['state'] for s in result['steps']], ['completed', 'unconfirmed', 'pending']
        )

    def test_cancel_during_operation_rejects_late_receipt_and_preserves_prior_result(self):
        run = WorkflowRun(flow(3))
        calls = []

        def execute(step, current, deadline, accept):
            calls.append(step)
            if len(calls) == 2:
                run.cancel()
                self.assertFalse(current())
                accept(receipt('too late'))
            return receipt()

        result = run.run(execute, current=lambda: True)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(
            [s['state'] for s in result['steps']], ['completed', 'unconfirmed', 'pending']
        )
        self.assertEqual(result['steps'][0]['result']['text'], '实际结果')

    def test_final_receipt_survives_cleanup_error_and_late_narration_stop(self):
        run = WorkflowRun(flow(1))

        def execute(step, current, deadline, accept):
            self.assertTrue(accept(receipt()))
            self.assertFalse(run.cancel())
            self.assertFalse(accept(receipt()))
            raise OSError('cleanup failure after result receipt')

        result = run.run(execute, current=lambda: True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['steps'][0]['result']['text'], '实际结果')
        self.assertFalse(run.accept(flow(1)['steps'][0]['id'], receipt()))

    def test_arbitrary_text_or_wrong_receipt_never_means_success(self):
        for value in (
            '已完成',
            None,
            receipt(kind='web_response'),
            StepOutput('assistant_turn', 'x', 'x', False),
        ):
            with self.subTest(value=value):
                run = WorkflowRun(flow(1))
                result = run.run(lambda *args: value, current=lambda: True)
                self.assertEqual(result['state'], 'failed')
                self.assertIsNone(result['steps'][0]['result'])

    def test_unbound_scope_fails_before_any_network_or_camera(self):
        calls = []
        result = WorkflowRun(builtins()[0]).run(lambda *a: calls.append(a), current=lambda: True)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(calls, [])

    def test_camera_requires_fresh_matching_authorization_and_never_schedules(self):
        value = builtins()[2]
        for decision, expected in ((False, 'failed'), (1, 'failed'), (True, 'completed')):
            auth, calls = [], []

            def authorize(flow_hash, step_id):
                auth.append((flow_hash, step_id))
                return decision

            def execute(*args):
                calls.append(True)
                return receipt(kind='vision_turn')

            result = WorkflowRun(value).run(
                execute, current=lambda: True, authorize_camera=authorize
            )
            self.assertEqual(auth, [(fingerprint(value), value['steps'][0]['id'])])
            self.assertEqual(result['state'], expected)
            self.assertEqual(len(calls), int(decision is True))
        with self.assertRaises(ValueError):
            WorkflowRun(value, origin='scheduled')

    def test_camera_authorization_does_not_survive_owner_change(self):
        owned, calls = [True], []

        def authorize(*args):
            owned[0] = False
            return True

        result = WorkflowRun(builtins()[2]).run(
            lambda *a: calls.append(a), current=lambda: owned[0], authorize_camera=authorize
        )
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(calls, [])

    def test_owner_must_be_literal_true_and_deadline_is_checked_before_effects(self):
        for current, now, expected in (
            (lambda: 1, [0, 0], 'cancelled'),
            (lambda: True, [0, 500], 'failed'),
        ):
            calls = []
            run = WorkflowRun(flow(1), clock=lambda: now.pop(0) if len(now) > 1 else now[0])
            result = run.run(lambda *a: calls.append(a), current=current, budget=1)
            self.assertEqual(result['state'], expected)
            self.assertEqual(calls, [])

    def test_mutating_input_cannot_change_prepared_plan(self):
        value = flow(1)
        run = WorkflowRun(value)
        value['steps'][0]['text'] = 'new side effect'
        result = run.run(lambda step, *a: receipt(step['text']), current=lambda: True)
        self.assertEqual(result['steps'][0]['result']['text'], '任务1')
        result['steps'][0]['result']['text'] = 'changed externally'
        self.assertEqual(run.snapshot(include_results=True)['steps'][0]['result']['text'], '任务1')

    def test_scheduled_run_binds_claim_definition_and_only_safe_read_steps(self):
        value = builtins()[0]
        value['steps'] = value['steps'][:1]
        value['schedule'] = dict(kind='interval', enabled=True, configured_at=100, seconds=300)
        attempt = dict(
            id='a' * 32,
            workflow_id=value['id'],
            definition_hash=fingerprint(value),
            execution='claimed',
            delivery='not_started',
        )
        run = WorkflowRun(value, origin='scheduled', attempt=attempt)
        result = run.run(lambda *a: receipt(kind='web_response'), current=lambda: True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['id'], attempt['id'])
        for field, changed in (
            ('definition_hash', 'f' * 64),
            ('workflow_id', 'b' * 32),
            ('execution', 'completed'),
        ):
            bad = deepcopy(attempt)
            bad[field] = changed
            with self.assertRaises(ValueError):
                WorkflowRun(value, origin='scheduled', attempt=bad)

    def test_result_clips_bounded_text_and_rejects_bad_encoding(self):
        output = receipt('字' * 9000).normalized('assistant')
        self.assertEqual(len(output['text']), 8000)
        self.assertTrue(output['truncated'])
        for value in ('\ud800', '\x00', 'x\x1f', '', '   '):
            with self.assertRaises((ValueError, UnicodeError, WorkflowUnconfirmed)):
                receipt(value).normalized('assistant')

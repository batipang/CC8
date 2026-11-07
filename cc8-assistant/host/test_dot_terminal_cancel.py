"""Cancel exact observed server handles without killing other work or replaying."""

import threading
import unittest
from unittest.mock import Mock, patch

from dot_client import DotBrain, Turn
from dot_receipt import DotError
from dot_terminals import CommandScope, stop_owned_terminals
from minimax_client import Cancelled
from test_dot_client import FakeRpc
from workspace import Workspace


def command(scope, item='owned-item', turn='own-turn', thread='own-thread', **extra):
    scope.observe(
        {
            'method': 'item/started',
            'params': {
                'threadId': thread,
                'turnId': turn,
                'item': {'type': 'commandExecution', 'id': item, **extra},
            },
        }
    )


class TerminalScopeTests(unittest.TestCase):
    def scope(self):
        scope = CommandScope('own-thread')
        command(scope)
        scope.bind('own-turn')
        return scope

    def test_early_receipts_and_other_turns_never_expand_cancel_scope(self):
        scope = CommandScope('own-thread')
        command(scope)
        command(scope, 'old-item', 'old-turn')
        command(scope, 'foreign', thread='foreign')
        scope.bind('own-turn')
        command(scope, 'later', 'new-turn')
        self.assertEqual(scope.snapshot('own-turn'), {'owned-item'})
        self.assertFalse(scope.snapshot('new-turn'))
        self.assertNotIn('command', vars(scope))

    def test_exact_items_only_after_complete_pagination(self):
        scope = self.scope()
        calls = []

        def request(method, params, timeout):
            calls.append((method, params))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            if method.endswith('/list'):
                return (
                    {
                        'data': [{'processId': 'own-handle', 'itemId': 'owned-item'}],
                        'nextCursor': 'page2',
                    }
                    if params['cursor'] is None
                    else {'data': [{'processId': 'keep', 'itemId': 'previous-turn'}]}
                )
            return {'terminated': True}

        stop_owned_terminals(Mock(request=request), scope, 'own-turn')
        self.assertEqual(
            [m for m, _ in calls],
            [
                'thread/backgroundTerminals/list',
                'thread/backgroundTerminals/list',
                'thread/backgroundTerminals/terminate',
            ],
        )
        self.assertEqual(calls[-1][1], {'threadId': 'own-thread', 'processId': 'own-handle'})

    def test_incomplete_ambiguous_or_oversized_inventory_never_terminates(self):
        own = {'processId': 'h', 'itemId': 'owned-item'}
        for response in (
            {},
            {'data': None},
            {'data': [own, own]},
            {'data': [own, {'processId': 'h', 'itemId': 'other'}]},
            {'data': [own], 'nextCursor': ''},
            {'data': [own], 'nextCursor': 'repeat'},
            {'data': [own] * 65},
            {'data': [None]},
        ):
            with self.subTest(response=response):
                rpc = Mock()
                rpc.request.return_value = response
                stop_owned_terminals(rpc, self.scope(), 'own-turn')
                self.assertTrue(
                    all(c.args[0].endswith('/list') for c in rpc.request.call_args_list)
                )

    def test_inventory_failure_and_termination_timeout_never_retries(self):
        for failure_index in (0, 1):
            with self.subTest(failure_index=failure_index):
                rpc = Mock()
                rpc.request.side_effect = (
                    [DotError('unsupported')]
                    if failure_index == 0
                    else [
                        {'data': [{'processId': 'h', 'itemId': 'owned-item'}]},
                        DotError('timeout'),
                    ]
                )
                with self.assertRaises(DotError):
                    stop_owned_terminals(rpc, self.scope(), 'own-turn')
                self.assertEqual(rpc.request.call_count, failure_index + 1)

    def test_scope_overflow_is_closed_and_empty_scope_does_no_queries(self):
        scope = self.scope()
        for i in range(257):
            command(scope, str(i))
        self.assertTrue(scope.overflow)
        rpc = Mock()
        stop_owned_terminals(rpc, scope, 'own-turn')
        rpc.request.assert_not_called()
        self.assertEqual(scope.items, {})

    def test_wall_budget_prevents_late_mutation(self):
        rpc = Mock()
        rpc.request.return_value = {'data': [{'processId': 'h', 'itemId': 'owned-item'}]}
        with (
            patch('dot_terminals.time.monotonic', side_effect=[0, 0, 5]),
            self.assertRaises(DotError),
        ):
            stop_owned_terminals(rpc, self.scope(), 'own-turn')
        self.assertEqual(rpc.request.call_count, 1)


class ObservableRpc(FakeRpc):
    def __init__(self):
        super().__init__()
        self.listeners = []

    def listen(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def emit(self, message):
        for listener in tuple(self.listeners):
            listener(message)

    def request(self, method, params, timeout=15):
        if method == 'thread/backgroundTerminals/list':
            self.calls.append((method, params))
            return {
                'data': [
                    {'processId': 'current-process', 'itemId': 'current-item'},
                    {'processId': 'old-process', 'itemId': 'old-item'},
                ]
            }
        return super().request(method, params, timeout)


class ClientIntegrationTests(unittest.TestCase):
    def test_overtaken_callback_does_not_terminate_reused_server_handle(self):
        rpc = Mock()
        rpc.request.return_value = {}
        brain = DotBrain(rpc)
        scope = CommandScope('own-thread')
        command(scope)
        scope.bind('own-turn')
        old = Turn(ident='own-turn', thread_id='own-thread', rpc=rpc, terminals=scope)
        brain.latest_rpc_turn = Turn(ident='new-turn')
        brain._interrupt(old)
        self.assertEqual([c.args[0] for c in rpc.request.call_args_list], ['turn/interrupt'])

    def test_new_turn_waits_for_owned_handle_cleanup_before_admission(self):
        rpc = ObservableRpc()
        brain = DotBrain(rpc)
        brain.thread_id = 'voice-thread'
        scope = CommandScope('voice-thread')
        command(scope, 'current-item', thread='voice-thread')
        scope.bind('own-turn')
        old = Turn(ident='own-turn', thread_id='voice-thread', rpc=rpc, terminals=scope)
        brain.latest_rpc_turn = old
        listed, release, entered, done, attempted = (threading.Event() for _ in range(5))
        original = rpc.request
        failures = []

        def request(method, params, timeout=15):
            if method.endswith('/list'):
                listed.set()
                if not release.wait(3):
                    raise AssertionError('Cleanup barrier expired')
            if method == 'turn/start':
                entered.set()
            return original(method, params, timeout)

        rpc.request = request

        def next_turn():
            try:
                list(
                    brain.clauses(
                        'new explicit request',
                        [],
                        on_web=lambda kind, value: (
                            attempted.set()
                            if kind == 'brain_stage' and value.get('stage') == 'dot_turn_start'
                            else None
                        ),
                    )
                )
            except BaseException as error:
                failures.append(error)
            finally:
                done.set()

        cancel = threading.Thread(target=brain._interrupt, args=(old,))
        followup = threading.Thread(target=next_turn)
        cancel.start()
        try:
            self.assertTrue(listed.wait(1))
            followup.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(entered.wait(0.05))
        finally:
            release.set()
            cancel.join(3)
            if followup.ident is not None:
                followup.join(3)
                if followup.is_alive():
                    brain.cancel()
                    followup.join(3)
        self.assertFalse(cancel.is_alive())
        self.assertTrue(done.is_set())
        self.assertEqual(failures, [])
        methods = [m for m, p in rpc.calls]
        self.assertLess(
            methods.index('thread/backgroundTerminals/terminate'), methods.index('turn/start')
        )

    def test_cancel_before_ack_cleans_observed_own_terminal_only(self):
        rpc = ObservableRpc()
        brain = DotBrain(rpc)

        def before_ack():
            rpc.emit(
                {
                    'method': 'item/started',
                    'params': {
                        'threadId': 'voice-thread',
                        'turnId': 'turn-1',
                        'item': {'type': 'commandExecution', 'id': 'current-item'},
                    },
                }
            )
            brain.current.stopped.set()

        rpc.before_ack = before_ack
        with self.assertRaises(Cancelled):
            list(brain.clauses('cancel own test', []))
        self.assertIn(
            (
                'thread/backgroundTerminals/terminate',
                {'threadId': 'voice-thread', 'processId': 'current-process'},
            ),
            rpc.calls,
        )
        self.assertFalse(any(p.get('processId') == 'old-process' for m, p in rpc.calls))
        self.assertEqual(rpc.listeners, [])

    def test_delayed_interrupt_keeps_original_rpc_and_thread(self):
        old, new = Mock(), Mock()
        old.request.return_value = {}
        brain = DotBrain(old)
        brain.thread_id = 'original-thread'
        brain.current = Turn(ident='original-turn')
        queued = []

        def deferred(*, target, args, daemon):
            queued.append((target, args))
            return Mock(start=Mock())

        with patch('dot_client.threading.Thread', side_effect=deferred):
            brain.cancel()
        brain.rpc, brain.thread_id, brain.current = new, 'new-thread', Turn()
        queued[0][0](*queued[0][1])
        new.request.assert_not_called()
        old.request.assert_called_once_with(
            'turn/interrupt', {'threadId': 'original-thread', 'turnId': 'original-turn'}, timeout=4
        )

    def test_receipt_completed_never_cleans_background_services(self):
        rpc = Mock()
        brain = DotBrain(rpc)
        turn = Turn(ident='done', thread_id='own-thread')
        turn.receipt_completed.set()
        brain._interrupt(turn)
        rpc.request.assert_not_called()

    def test_cancelled_request_never_claims_all_tools_stopped(self):
        for unconfirmed in (False, True):
            with self.subTest(unconfirmed=unconfirmed):
                board = Workspace()
                board.task_start(5, 'own test')
                board.cancel_generation(5, unconfirmed=unconfirmed)
                stage = board.snapshot()['tasks'][0]['stage']
                self.assertIn('可能仍在运行', stage)
                self.assertIn('电脑核对', stage)

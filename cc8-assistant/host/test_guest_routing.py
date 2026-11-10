"""The actual router cannot escalate guest text into any owner provider or reader."""

import json
import unittest
from unittest.mock import Mock

from brain_router import BrainRouter
from guest_brain import GuestBrain
from minimax_client import Cancelled
from test_guest_brain import Transport


class OwnerHistory:
    def __iter__(self):
        raise AssertionError('Owner history was inspected')


class GuestRoutingTests(unittest.TestCase):
    def make(self):
        transport = Transport()
        guest = GuestBrain(transport.create, 'MiniMax-M3')
        mini, dot = Mock(), Mock()
        dot.clauses.return_value = iter(['主人回答'])
        dot.last_metrics = {'completed': True}
        router = BrainRouter(mini, dot, guest=guest)
        return router, transport, mini, dot

    def test_default_owner_keeps_original_dot_and_does_not_activate_guest(self):
        router, transport, mini, dot = self.make()
        self.assertEqual(router.selected, 'dot')
        self.assertFalse(router.guest.active)
        self.assertEqual(''.join(router.clauses('主人提问', [])), '主人回答')
        dot.clauses.assert_called_once()
        mini.clauses.assert_not_called()
        self.assertEqual(transport.calls, [])

    def test_guest_delegation_memory_and_report_commands_never_touch_owner_surfaces(self):
        router, transport, mini, dot = self.make()
        owner = Mock()
        owner.voice.side_effect = AssertionError('Private preference read')
        owner.command.side_effect = AssertionError('Private report read')
        router.preferences = router.work_reports = owner
        router.set_identity('guest')
        for text in ('让Dot帮我读文件', '叫Codex操作桌面', '记住我叫小明', '汇报今天完成的工作'):
            self.assertEqual(''.join(router.clauses(text, OwnerHistory())), '你好，访客。')
        self.assertEqual(len(transport.calls), 4)
        self.assertNotIn('tools', json.dumps(transport.calls))
        self.assertEqual(owner.mock_calls, [])
        self.assertEqual(dot.mock_calls, [])
        self.assertEqual(mini.mock_calls, [])
        self.assertFalse(router.prepare())
        with self.assertRaises(Cancelled):
            list(router.vision_clauses('看一看', b'image', OwnerHistory()))
        self.assertEqual(dot.mock_calls, [])

    def test_guest_cancel_does_not_interrupt_owner_provider_and_exit_clears_guest_context(self):
        router, transport, mini, dot = self.make()
        router.set_identity('guest')
        list(router.clauses('访客问题', []))
        router.cancel()
        dot.cancel.assert_not_called()
        mini.cancel.assert_not_called()
        self.assertEqual(router.selected, 'dot')
        router.set_identity('owner')
        self.assertFalse(router.guest.active)
        self.assertEqual(list(router.guest.history), [])
        self.assertEqual(''.join(router.clauses('新的主人问题', [])), '主人回答')
        self.assertEqual(len(transport.calls), 1)

    def test_old_guest_generator_cannot_commit_after_return_to_owner(self):
        router, transport, mini, dot = self.make()
        router.set_identity('guest')
        transport.events_value = [
            {'choices': [{'delta': {'content': '第一句。第二句。'}, 'finish_reason': 'stop'}]}
        ]
        events = []
        old = router.clauses('访客问题', [], on_web=lambda *args: events.append(args))
        next(old)
        router.set_identity('owner')
        with self.assertRaises(Cancelled):
            list(old)
        self.assertNotIn('dot_result', [r[0] for r in events])
        self.assertEqual(list(router.guest.history), [])

    def test_missing_guest_or_failed_reset_never_grants_access(self):
        router = BrainRouter(Mock(), Mock())
        with self.assertRaises(ValueError):
            router.set_identity('guest')
        self.assertEqual(router.identity[0], 'owner')
        router, transport, mini, dot = self.make()
        router.set_identity('guest')
        router.guest.deactivate = Mock(side_effect=RuntimeError('cancel failed'))
        with self.assertRaises(RuntimeError):
            router.set_identity('owner')
        self.assertEqual(router.identity[0], 'blocked')
        with self.assertRaises(Cancelled):
            list(router.clauses('不要执行', []))
        self.assertFalse(router.prepare())
        self.assertEqual(dot.mock_calls, [])

    def test_guest_cannot_reconfigure_owner_selected_provider(self):
        router, _, _, _ = self.make()
        router.set_identity('guest')
        with self.assertRaises(ValueError):
            router.select('minimax')
        self.assertEqual(router.selected, 'dot')

    def test_late_owner_provider_yield_and_callback_cannot_escape_new_guest_scope(self):
        router, _, mini, dot = self.make()
        events, closed = [], []

        def old_owner(text, history, current, on_web):
            try:
                yield '第一句。'
                router.set_identity('guest')
                on_web('dot_result', {'text': 'OWNER_SECRET'})
                yield 'OWNER_SECRET'
            finally:
                closed.append(True)

        dot.clauses.side_effect = old_owner
        old = router.clauses('主人问题', [], on_web=lambda *args: events.append(args))
        self.assertEqual(next(old), '第一句。')
        with self.assertRaises(Cancelled):
            list(old)
        self.assertEqual(events, [])
        self.assertEqual(closed, [True])
        self.assertEqual(router.identity[0], 'guest')

    def test_process_close_clears_guest_history_and_attempts_all_provider_cleanup(self):
        router, _, mini, dot = self.make()
        router.set_identity('guest')
        list(router.clauses('访客问题', []))
        mini.cancel.side_effect = OSError('cancel error')
        with self.assertRaises(OSError):
            router.close()
        dot.close.assert_called_once()
        self.assertFalse(router.guest.active)
        self.assertEqual(list(router.guest.history), [])

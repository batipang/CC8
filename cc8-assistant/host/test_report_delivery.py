"""Report routing, bounded rereads and actual device-receipt admission."""

from copy import deepcopy
import threading
import unittest
from unittest.mock import patch
from dot_client import Cancelled, DotError
from dot_handoff import ChatCatalog, Handoff
from report_progress import DeliveryToken
from report_time import ReportRange
from report_voice import ReportCommand, ReportVoice, ReportVoiceHooks, report_command
from reply_types import Reply
from test_report_voice import SOURCE, report
import test_report_voice as voice_fixtures
from test_report_ranges import stamp
import test_failure_feedback as response_fixtures
from test_work_reports import Fixture, turn, final
from work_reports import ReportWindow


class ExtendedVoiceTests(unittest.TestCase):
    def session(self):
        self.context = [SOURCE, object(), 0]
        self.now = [300]
        self.reads = []
        self.data = report('甲', '乙', '丙', '丁')

        def read(source, window, current):
            self.assertIs(source, SOURCE)
            self.assertTrue(current())
            self.reads.append(window)
            result = deepcopy(self.data)
            if isinstance(window, ReportWindow):
                result['window_start'], result['window_end'] = window.start, window.end
            return result

        return ReportVoice(
            ReportVoiceHooks(threading.RLock(), lambda: tuple(self.context), read),
            clock=lambda: 0,
            wall_clock=lambda: self.now[0],
        )

    def answer(self, voice, text):
        return voice.answer(voice.command(text), lambda: True)

    def confirm(self, voice, reply):
        for token in reply.delivery:
            if token is not None:
                voice.confirm(token)

    def test_aliases_and_custom_intents_remain_local_and_compound_actions_are_guarded(self):
        for text in (
            '汇报上次汇报后的工作',
            '总结上次汇报之后的工作成果',
            '查看自上次汇报以来的工作',
            '汇报从上次汇报到现在的成果',
        ):
            self.assertEqual(report_command(text), ReportCommand('report', 'since_last'))
        self.assertEqual(report_command('大黄蜂，汇报今天上午八点到上午十点的工作').kind, 'custom')
        self.assertEqual(
            report_command('汇报今天08:00到10:00的工作然后继续第二项').kind, 'continue'
        )

    def test_custom_window_frozen_and_returned_bounds_must_match(self):
        voice = self.session()
        self.now[0] = stamp(9, 20)
        self.data = report()
        result = self.answer(voice, '汇报今天上午八点到上午十点的工作')
        self.assertEqual(self.reads, [ReportWindow(stamp(9, 8), stamp(9, 10))])
        self.assertIn('2026-11-09 08:00:00', result.text)

    def test_mismatched_range_and_duplicate_result_are_unconfirmed(self):
        for duplicate in (False, True):
            voice = self.session()
            data = report('甲')
            if duplicate:
                data['window_end'] = 300
                self.assertTrue(voice.valid_report(SOURCE, data))
                data['items'].append(deepcopy(data['items'][0]))
            voice.hooks = ReportVoiceHooks(voice.hooks.lock, voice.hooks.context, lambda *a: data)
            with self.assertRaises(DotError):
                voice.answer(
                    ReportCommand('custom', ReportRange('1970-01-01T08:01:40', '08:05:00')),
                    lambda: True,
                )
            self.assertIsNone(voice.snapshot)

    def test_missing_baseline_and_ambiguous_custom_never_read(self):
        voice = self.session()
        for text in ('汇报上次汇报后的工作', '汇报今天八点到十点的工作'):
            self.assertEqual(self.answer(voice, text).operation, 'clarification')
        self.assertEqual(self.reads, [])

    def test_since_rescans_origin_retains_unplayed_and_collects_late_results(self):
        voice = self.session()
        overview = self.answer(voice, '汇报今天的工作')
        self.confirm(voice, overview)
        self.assertEqual(voice.progress.status()['pending'], 1)
        self.data = report('甲', '乙', '丙', '丁', '后来收集')
        result = self.answer(voice, '汇报上次汇报后的工作')
        self.assertEqual(self.reads[-1], ReportWindow(100, 300))
        self.assertIn('第1项 · 原答复摘录', result.text)
        self.assertIn('丁', result.text)
        self.assertIn('本次才收集到', result.text)
        self.assertEqual(result.items, 2)
        self.assertEqual(voice.progress.status()['pending'], 2)
        self.confirm(voice, result)
        self.now[0] = 400
        empty = self.answer(voice, '汇报上次汇报后的工作')
        self.assertIn('没有新增或待补播', empty.text)
        self.assertNotIn('没有本窗口', empty.text)
        self.assertEqual(voice.progress.status()['summary_delivered'], 5)

    def test_ordinary_topic_and_transport_change_keep_delivery_but_clear_ordinals(self):
        voice = self.session()
        initial = self.answer(voice, '汇报今天的工作')
        self.confirm(voice, initial)
        voice.forget()
        self.context[1], self.context[2] = object(), 2
        self.assertIsNone(voice.command('第二项'))
        self.assertEqual(self.answer(voice, '汇报上次汇报后的工作').items, 1)
        self.context[0] = Handoff('different-chat', '另一个来源', (), False)
        notice = self.answer(voice, '汇报上次汇报后的工作')
        self.assertIn('尚无本来源', notice.text)
        self.assertIsNone(voice.progress)

    def test_clock_rollback_age_and_failure_preserve_prior_unplayed_ledger(self):
        voice = self.session()
        self.answer(voice, '汇报今天的工作')
        initial = voice.progress
        for now in (199, 100 + 32 * 86400):
            self.now[0] = now
            self.assertEqual(self.answer(voice, '汇报上次汇报后的工作').operation, 'clarification')
            self.assertIs(voice.progress, initial)
        self.now[0] = 300
        voice.hooks = ReportVoiceHooks(
            voice.hooks.lock,
            voice.hooks.context,
            lambda *args: (_ for _ in ()).throw(DotError('private')),
        )
        with self.assertRaises(DotError):
            self.answer(voice, '汇报上次汇报后的工作')
        self.assertIs(voice.progress, initial)
        self.assertEqual(initial.status()['pending'], 4)

    def test_cancelled_read_and_late_token_cannot_commit_new_delivery(self):
        voice = self.session()
        reply = self.answer(voice, '汇报今天的工作')
        initial = voice.progress

        def read(*args):
            voice.forget()
            result = deepcopy(self.data)
            result['window_end'] = 300
            return result

        voice.hooks = ReportVoiceHooks(voice.hooks.lock, voice.hooks.context, read)
        with self.assertRaises(Cancelled):
            self.answer(voice, '汇报上次汇报后的工作')
        self.confirm(voice, reply)
        self.assertIs(voice.progress, initial)
        self.assertEqual(initial.status()['pending'], 4)

    def test_detail_requires_every_clause_and_invalid_tokens_do_not_mark_delivery(self):
        voice = self.session()
        self.answer(voice, '汇报今天的工作')
        reply = self.answer(voice, '详细说说第四项')
        for token in reply.delivery[:-1]:
            voice.confirm(token)
        self.assertEqual(voice.progress.status()['pending'], 4)
        token = reply.delivery[-1]
        voice.confirm(DeliveryToken(token.plan, True))
        voice.confirm(DeliveryToken('other', token.index))
        self.assertEqual(voice.progress.status()['pending'], 4)
        voice.confirm(token)
        self.assertEqual(voice.progress.status()['pending'], 3)

    def test_capacity_failure_preserves_snapshot_and_all_pending(self):
        voice = self.session()
        self.answer(voice, '汇报今天的工作')
        previous = voice.snapshot
        self.data = report('甲', '乙', '丙', '丁', '新增')
        with patch('report_progress.MAX_RECORDS', 4):
            reply = self.answer(voice, '汇报上次汇报后的工作')
        self.assertIn('容量', reply.text)
        self.assertIs(voice.snapshot, previous)
        self.assertEqual(voice.progress.status()['pending'], 4)
        self.assertIn('丁', self.answer(voice, '详细说说第四项').text)


class DeviceReceiptTests(unittest.TestCase):
    def session(self):
        _, bridge = voice_fixtures.IntegrationTests().session()
        bridge.chats.catalog.report = lambda *a, **k: report('甲', '乙', '丙', '丁')
        return bridge

    def run_report(self, bridge):
        response_fixtures.FailureFeedbackTests().run_one(
            bridge.response, Reply(bridge.generation, '汇报今天的工作')
        )

    def test_speak_true_without_confirmed_device_receipt_never_advances_ledger(self):
        bridge = self.session()
        bridge.speak = lambda *a: True
        self.run_report(bridge)
        self.assertEqual(bridge.chats.voice.progress.status()['pending'], 4)

    def test_only_overview_item_clauses_confirmed_by_device_are_marked(self):
        bridge = self.session()

        def speak(text, generation):
            bridge.diagnostics.playback_done(generation, confirmed_ack_ms=0)
            return True

        bridge.speak = speak
        self.run_report(bridge)
        self.assertEqual(bridge.chats.voice.progress.status()['summary_delivered'], 3)
        self.assertEqual(bridge.chats.voice.progress.status()['pending'], 1)
        self.assertIsNone(bridge.brain.current_report_delivery)
        self.assertEqual(bridge.workspace.snapshot()['tasks'][0]['status'], 'completed')

    def test_stop_before_or_after_matching_ack_preserves_actual_delivery_only(self):
        for ack in (False, True):
            with self.subTest(ack=ack):
                bridge = self.session()

                def speak(text, generation):
                    if bridge.brain.current_report_delivery:
                        if ack:
                            bridge.diagnostics.playback_done(generation, confirmed_ack_ms=0)
                        bridge.session.generation += 1
                        return False
                    return True

                bridge.speak = speak
                self.run_report(bridge)
                self.assertEqual(
                    bridge.chats.voice.progress.status()['summary_delivered'], int(ack)
                )
                self.assertEqual(bridge.workspace.snapshot()['tasks'][0]['status'], 'completed')
                self.assertIsNone(bridge.brain.current_report_delivery)

    def test_ui_source_clear_erases_progress(self):
        helper, bridge = voice_fixtures.IntegrationTests().session()
        list(bridge.brain.clauses('汇报今天的工作', []))
        self.assertIsNotNone(bridge.chats.voice.progress)
        bridge.session.active = False
        helper.request(bridge, 'clear')
        helper.join(bridge)
        self.assertIsNone(bridge.chats.voice.progress)

    def test_catalog_custom_window_reaches_real_reader_with_boundary_filtering(self):
        fixture = Fixture()
        fixture.turn_pages[0]['data'] = [
            turn('left', completed=100),
            turn('last', completed=199),
            turn('right', completed=200),
        ]
        fixture.item_pages = {
            ident: [
                dict(
                    data=[dict(turnId=ident, item=final('final-' + ident, ident))], nextCursor=None
                )
            ]
            for ident in ('left', 'last', 'right')
        }
        result = ChatCatalog(fixture.factory).report(SOURCE, ReportWindow(100, 200))
        self.assertEqual({x['excerpt'] for x in result['items']}, {'left', 'last'})

    def test_wrong_generation_unconfirmed_and_late_ack_do_not_advance_counter(self):
        from diagnostics import TraceBook

        traces = TraceBook()
        traces.begin('text', 1)
        traces.begin('text', 2)
        traces.playback_done(1, confirmed_ack_ms=0)
        traces.playback_done(2, confirmed_ack_ms=None)
        self.assertEqual(traces.confirmed_playback_count(2), 0)
        traces.finish(2, 'cancelled')
        traces.playback_done(2, confirmed_ack_ms=0)
        self.assertEqual(traces.confirmed_playback_count(2), 0)
        self.assertEqual(traces.confirmed_playback_count(1), 1)


if __name__ == '__main__':
    unittest.main()

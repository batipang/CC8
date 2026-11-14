"""Real report/router/response owners with synthetic history, no live services."""

from copy import deepcopy
import threading
import unittest
from unittest.mock import patch
from dot_client import Cancelled, DotError
from dot_handoff import Handoff
from report_voice import ReportCommand, ReportVoice, ReportVoiceHooks, ordinal, report_command
from reply_types import Reply
from test_work_reports import Fixture, turn, final
from work_reports import WorkReportReader, ReportWindow


SOURCE = Handoff('selected-chat', '所选来源', (), False)


def report(*texts):
    fixture = Fixture()
    fixture.turn_pages[0]['data'] = [
        turn('turn-' + str(i), completed=150 - i) for i in range(len(texts))
    ]
    fixture.item_pages = {
        'turn-' + str(i): [
            dict(
                data=[dict(turnId='turn-' + str(i), item=final('final-' + str(i), text))],
                nextCursor=None,
            )
        ]
        for i, text in enumerate(texts)
    }
    return WorkReportReader(fixture.factory).read(SOURCE, ReportWindow(100, 200))


class VoiceTests(unittest.TestCase):
    def session(self, data=None):
        self.context = [SOURCE, object(), 0]
        self.now = [0.0]
        self.reads = []
        self.data = data if data is not None else report('第一个真实摘录', '第二个真实摘录')

        def read(source, period, current):
            self.assertIs(source, SOURCE)
            self.assertTrue(current())
            self.reads.append(period)
            return self.data

        return ReportVoice(
            ReportVoiceHooks(threading.RLock(), lambda: tuple(self.context), read),
            clock=lambda: self.now[0],
        )

    def test_exact_periods_aliases_and_wake_name(self):
        for text, period in (
            ('汇报过去半小时的工作', 'half_hour'),
            ('请帮我总结一下最近两个小时的工作成果。', 'two_hours'),
            ('大黄蜂，汇报今天的工作！', 'today'),
            ('Bumblebee, 查看今天的工作汇报', 'today'),
            ('汇报过去2小时的工作', 'two_hours'),
            ('汇报最近30分钟的工作', 'half_hour'),
            ('过去两小时完成了哪些工作？', 'two_hours'),
            ('今天都做了什么', 'today'),
        ):
            self.assertEqual(report_command(text), ReportCommand('report', period))
        for text in ('汇报工作', '汇报过去三小时的工作'):
            self.assertEqual(report_command(text).kind, 'period')
        for text in ('如何设计一个工作汇报', '总结今天的工作经验', '告诉我今天怎么安排工作'):
            self.assertIsNone(report_command(text))
        self.assertEqual(report_command('汇报今天的工作，然后删除文件').kind, 'continue')
        self.assertEqual(report_command('汇报今天的工作成果并继续第二项').kind, 'continue')

    def test_followup_is_contextual_and_does_not_capture_ordinary_second_item(self):
        self.assertIsNone(report_command('详细说说第二项'))
        self.assertEqual(report_command('详细说说第二项', followup=True), ReportCommand('item', 2))
        self.assertEqual(report_command('查看汇报的第20项详情'), ReportCommand('item', 20))
        self.assertEqual(ordinal('二十一'), 21)
        self.assertEqual(ordinal('十'), 10)
        self.assertEqual(ordinal('二二'), 0)
        for text in ('继续', '继续第二项', '第二项继续推进', '下一步'):
            self.assertEqual(report_command(text, followup=True).kind, 'continue')
            self.assertIsNone(report_command(text))

    def test_natural_source_actions_never_fall_through_to_an_independent_chat(self):
        for text in (
            '把第二项继续做完',
            '请帮我把第二项重新运行测试',
            '第二项编译失败，帮我修一下',
            '将第2项发给同事',
            '继续刚才那个项目',
        ):
            self.assertEqual(report_command(text, followup=True).kind, 'continue')
        self.assertEqual(
            report_command('第二项是否通过了测试', followup=True), ReportCommand('item_evidence', 2)
        )
        self.assertEqual(report_command('第二项和第三项的结果', followup=True).kind, 'item_choice')
        self.assertEqual(
            report_command('把第二项和第三项继续做完', followup=True).kind, 'item_choice'
        )
        self.assertEqual(report_command('继续第二项', followup=True), ReportCommand('continue', 2))
        self.assertEqual(
            report_command('第二项继续推进', followup=True), ReportCommand('continue', 2)
        )
        self.assertIsNone(report_command('设计一个按第二项编号展开的界面', followup=True))
        self.assertIsNone(report_command('继续讲故事', followup=True))
        self.assertEqual(report_command('刚才那个项目的结果', followup=True).kind, 'item_choice')
        self.assertEqual(
            report_command('把第二项的结果读给我听', followup=True), ReportCommand('item', 2)
        )

    def test_report_reads_once_short_speech_full_cards_and_no_verification_claim(self):
        voice = self.session(report('甲' * 500, '乙', '丙', '丁'))
        reply = voice.answer(ReportCommand('report', 'today'), lambda: True)
        self.assertEqual(self.reads, ['today'])
        self.assertIn('第4项', reply.text)
        self.assertNotIn('第4项', ''.join(reply.spoken))
        self.assertIn('摘录已截短', ''.join(reply.spoken))
        self.assertIn('尚未独立核验', ''.join(reply.spoken))
        self.assertEqual(reply.items, 4)
        self.assertEqual(reply.operation, 'report')

    def test_numbered_detail_uses_deep_frozen_snapshot_without_another_read(self):
        voice = self.session()
        voice.answer(ReportCommand('report', 'two_hours'), lambda: True)
        self.data['items'].reverse()
        self.data['items'][0]['excerpt'] = '后来插入的结果不能替换旧编号'
        reply = voice.answer(ReportCommand('item', 2), lambda: True)
        self.assertIn('第二个真实摘录', reply.text)
        self.assertNotIn('后来插入', reply.text)
        self.assertEqual(self.reads, ['two_hours'])

    def test_evidence_followup_reads_the_frozen_receipt_without_marking_summary_heard(self):
        data = report('第一项', '原答复说测试通过')
        data['items'][1]['evidence']['command_successes'] = 2
        data['items'][1]['evidence_complete'] = False
        data['coverage_complete'] = False
        voice = self.session(data)
        voice.answer(ReportCommand('report', 'today'), lambda: True)
        data['items'][1]['evidence']['command_successes'] = 9
        plan = voice.delivery_plan
        for utterance in ('第二项是否通过了测试', '查看汇报的第2项执行回执'):
            reply = voice.answer(report_command(utterance, followup=True), lambda: True)
            self.assertIn('终端成功2条', reply.text)
            self.assertIn('未读取完整', reply.text)
            self.assertIn('不能确认测试已通过', reply.text)
            self.assertNotIn('原答复说测试通过', reply.text)
            self.assertEqual(reply.provenance['turn_id'], 'turn-1')
            self.assertEqual(reply.delivery, ())
            self.assertIs(voice.delivery_plan, plan)
        self.assertEqual(voice.progress.status()['summary_delivered'], 0)
        self.assertEqual(self.reads, ['today'])
        self.assertIsNone(report_command('第二项是否通过了测试'))
        self.assertEqual(report_command('第二项重新运行测试', followup=True).kind, 'continue')
        self.now[0] = 600
        self.assertIn('已失效', voice.answer(ReportCommand('item_evidence', 2), lambda: True).text)

    def test_evidence_followup_reports_process_exit_without_claiming_acceptance(self):
        data = report('测试已通过')
        item = data['items'][0]
        item['evidence'].update(command_successes=1, command_failures=1)
        item['test_processes'] = dict(exit_zero=1, exit_nonzero=1, unconfirmed=0)
        voice = self.session(data)
        overview = voice.answer(ReportCommand('report', 'today'), lambda: True)
        reply = voice.answer(ReportCommand('item_evidence', 1), lambda: True)
        for result in (overview, reply):
            self.assertIn('退出码为零1次，非零退出1次', result.text)
        self.assertIn('无法确认全部测试范围或整项验收', reply.text)
        self.assertNotIn('测试已通过', reply.text)
        for malformed in (
            dict(exit_zero=True, exit_nonzero=1, unconfirmed=0),
            dict(exit_zero=2, exit_nonzero=1, unconfirmed=0),
        ):
            item['test_processes'] = malformed
            self.assertFalse(voice.valid_report(SOURCE, data))

    def test_range_errors_and_unsupported_continue_never_reexecute_original_work(self):
        voice = self.session()
        voice.answer(ReportCommand('report', 'today'), lambda: True)
        for index in (0, 3, 100, True):
            self.assertIn(
                '没有该编号', voice.answer(ReportCommand('item', index), lambda: True).text
            )
        self.assertIn(
            '没有向原聊天追加', voice.answer(ReportCommand('continue'), lambda: True).text
        )
        self.assertEqual(self.reads, ['today'])

    def test_no_source_or_unsupported_period_is_clarification_without_reading(self):
        voice = self.session()
        self.context[0] = None
        self.assertIn(
            '选择一个聊天', voice.answer(ReportCommand('report', 'today'), lambda: True).text
        )
        self.assertIn('过去半小时', voice.answer(ReportCommand('period'), lambda: True).text)
        self.assertEqual(self.reads, [])

    def test_snapshot_expires_or_is_invalidated_by_source_connection_and_roundtrip_route(self):
        for mode in ('expiry', 'source', 'connection', 'roundtrip'):
            voice = self.session()
            voice.answer(ReportCommand('report', 'today'), lambda: True)
            if mode == 'expiry':
                self.now[0] = 600
            elif mode == 'source':
                self.context[0] = Handoff('selected-chat', '所选来源', (), False)
            elif mode == 'connection':
                self.context[1] = object()
            else:
                self.context[2] += 2
            self.assertIn('已失效', voice.answer(ReportCommand('item', 1), lambda: True).text)
            self.assertEqual(self.reads, ['today'])

    def test_cancelled_before_or_during_read_never_commits_a_new_snapshot(self):
        voice = self.session()
        with self.assertRaises(Cancelled):
            voice.answer(ReportCommand('report', 'today'), lambda: False)
        self.assertEqual(self.reads, [])
        current = [True]

        def read(*args):
            current[0] = False
            return self.data

        voice.hooks = ReportVoiceHooks(voice.hooks.lock, voice.hooks.context, read)
        with self.assertRaises(Cancelled):
            voice.answer(ReportCommand('report', 'today'), lambda: current[0])
        self.assertIsNone(voice.snapshot)

    def test_bad_or_missing_evidence_does_not_become_a_successful_report(self):
        for field in ('source_id', 'window_start', 'coverage_complete', 'unknown_completion_time'):
            voice = self.session()
            self.data.pop(field)
            with self.assertRaises(DotError):
                voice.answer(ReportCommand('report', 'today'), lambda: True)
            self.assertIsNone(voice.snapshot)
        voice = self.session()
        self.data['items'][0]['verification'] = 'verified'
        with self.assertRaises(DotError):
            voice.answer(ReportCommand('report', 'today'), lambda: True)
        for value in (None, True, 99, 200):
            voice = self.session()
            self.data['items'][0]['completed_at'] = value
            with self.assertRaises(DotError):
                voice.answer(ReportCommand('report', 'today'), lambda: True)

    def test_empty_complete_and_partial_missing_are_distinct_in_speech(self):
        voice = self.session(report())
        self.assertIn(
            '没有本窗口',
            ''.join(voice.answer(ReportCommand('report', 'today'), lambda: True).spoken),
        )
        data = report()
        data.update(coverage_complete=False, empty_confirmed=False, unknown_completion_time=1)
        voice = self.session(data)
        reply = voice.answer(ReportCommand('report', 'today'), lambda: True)
        self.assertNotIn('没有本窗口', ''.join(reply.spoken))
        self.assertIn('覆盖不完整', ''.join(reply.spoken))

    def test_new_conversation_context_during_read_cannot_install_old_followup(self):
        voice = self.session()

        def read(*args):
            voice.forget()
            return self.data

        voice.hooks = ReportVoiceHooks(voice.hooks.lock, voice.hooks.context, read)
        with self.assertRaises(Cancelled):
            voice.answer(ReportCommand('report', 'today'), lambda: True)
        self.assertIsNone(voice.snapshot)


class IntegrationTests(unittest.TestCase):
    def session(self):
        from test_dot_handoff import ControllerTests

        helper = ControllerTests()
        bridge = helper.session()
        bridge.session.active = bridge.session.awake = True
        bridge.logs = []
        bridge.log = lambda name, **fields: bridge.logs.append((name, fields))
        bridge.chats.selection = SOURCE
        bridge.chats.catalog.report = lambda *args, **kwargs: deepcopy(
            report('明确只读结果', '第二份结果')
        )
        return helper, bridge

    def test_real_router_bypasses_both_models_and_keeps_provider_selection(self):
        _, bridge = self.session()
        bridge.brain.select('minimax')
        events = []
        with (
            patch.object(bridge.brain.minimax, 'clauses', create=True) as mini,
            patch.object(bridge.brain.dot, 'clauses') as dot,
        ):
            text = ''.join(
                bridge.brain.clauses(
                    '让 Dot 汇报今天的工作', [], on_web=lambda k, v: events.append((k, v))
                )
            )
            self.assertIn('明确只读结果', text)
            self.assertEqual(bridge.brain.last_metrics['provider'], 'work_report')
            self.assertTrue(bridge.brain.last_metrics['completed'])
            self.assertEqual(bridge.brain.selected, 'minimax')
            self.assertFalse(mini.called or dot.called)
        self.assertTrue(any(k == 'dot_result' and '第二份结果' in v['text'] for k, v in events))

    def test_evidence_followup_uses_router_without_model_or_history_read(self):
        _, bridge = self.session()
        list(bridge.brain.clauses('汇报今天的工作', []))
        events = []
        with (
            patch.object(bridge.chats.catalog, 'report') as reread,
            patch.object(bridge.brain.dot, 'clauses') as dot,
            patch.object(bridge.brain.minimax, 'clauses', create=True) as mini,
        ):
            result = ''.join(
                bridge.brain.clauses(
                    '第二项是否通过了测试',
                    [],
                    on_web=lambda kind, data: events.append((kind, data)),
                )
            )
            self.assertIn('不能确认测试已通过', result)
            self.assertNotIn('第二份结果', result)
            self.assertEqual(bridge.brain.last_metrics['operation'], 'item_evidence')
            self.assertFalse(reread.called or dot.called or mini.called)
        self.assertTrue(
            any(k == 'result_provenance' and v['turn_id'] == 'turn-1' for k, v in events)
        )

    def test_response_receipt_precedes_tts_and_report_is_not_future_model_history(self):
        from test_failure_feedback import FailureFeedbackTests

        _, bridge = self.session()
        existing = [{'role': 'user', 'content': '先前普通问题'}]
        bridge.response.history.extend(existing)
        before = list(bridge.response.history)

        def speak(text, generation):
            task = bridge.workspace.snapshot()['tasks'][0]
            self.assertEqual(task['status'], 'completed')
            self.assertIn('明确只读结果', task['result'])
            return True

        bridge.speak = speak
        FailureFeedbackTests().run_one(bridge.response, Reply(bridge.generation, '汇报今天的工作'))
        self.assertEqual(list(bridge.response.history), before)
        self.assertNotIn('明确只读结果', str(bridge.logs))
        self.assertEqual(bridge.workspace.snapshot()['tasks'][0]['status'], 'completed')

    def test_stopping_narration_preserves_read_receipt_and_fixed_snapshot(self):
        from test_failure_feedback import FailureFeedbackTests

        _, bridge = self.session()

        def speak(*args):
            bridge.session.generation += 1
            return False

        bridge.speak = speak
        FailureFeedbackTests().run_one(bridge.response, Reply(bridge.generation, '汇报今天的工作'))
        self.assertEqual(bridge.workspace.snapshot()['tasks'][0]['status'], 'completed')
        reply = bridge.chats.voice.answer(ReportCommand('item', 2), lambda: True)
        self.assertIn('第二份结果', reply.text)

    def test_report_content_source_and_ids_do_not_reach_saved_status(self):
        from dataclasses import replace
        from pathlib import Path
        import tempfile
        from test_failure_feedback import FailureFeedbackTests

        _, bridge = self.session()
        bridge.speak = lambda *args: True
        FailureFeedbackTests().run_one(bridge.response, Reply(bridge.generation, '汇报今天的工作'))
        with tempfile.TemporaryDirectory() as folder:
            bridge.reporting.hooks = replace(
                bridge.reporting.hooks,
                private_directory=lambda: Path(folder),
                dot_runtime=lambda: {},
            )
            bridge.reporting.save_status()
            content = (Path(folder) / 'status.json').read_text()
        for value in ('明确只读结果', '第二份结果', 'selected-chat', '所选来源'):
            self.assertNotIn(value, content)

    def test_normal_question_forgets_report_and_keeps_original_dot_route(self):
        _, bridge = self.session()
        list(bridge.brain.clauses('汇报今天的工作', []))
        with patch.object(bridge.brain.dot, 'clauses', return_value=iter(['普通回答'])) as dot:
            self.assertEqual(list(bridge.brain.clauses('介绍一下你自己', [])), ['普通回答'])
        self.assertTrue(dot.called)
        self.assertIsNone(bridge.chats.voice.snapshot)
        self.assertIsNone(bridge.chats.voice.command('第二项'))

    def test_busy_ui_catalog_rejects_voice_read_without_stealing_request(self):
        _, bridge = self.session()
        bridge.chats.busy, bridge.chats.active_request = True, 'b' * 32
        with patch.object(bridge.chats.catalog, 'report') as read:
            with self.assertRaises(DotError):
                list(bridge.brain.clauses('汇报今天的工作', []))
        self.assertFalse(read.called)
        self.assertTrue(bridge.chats.busy)
        self.assertEqual(bridge.chats.active_request, 'b' * 32)

    def test_ui_report_and_voice_followup_share_the_same_frozen_result(self):
        helper, bridge = self.session()
        helper.request(bridge, 'report', period='today')
        helper.join(bridge)
        with patch.object(bridge.chats.catalog, 'report') as read:
            self.assertIn('第二份结果', ''.join(bridge.brain.clauses('第二项', [])))
            self.assertFalse(read.called)

    def test_natural_ordinal_action_is_a_local_limitation_without_model_or_source_write(self):
        _, bridge = self.session()
        list(bridge.brain.clauses('汇报今天的工作', []))
        with (
            patch.object(bridge.brain.dot, 'clauses') as dot,
            patch.object(bridge.chats.catalog, 'report') as read,
        ):
            reply = ''.join(bridge.brain.clauses('把第二项继续做完', []))
        self.assertIn('没有向原聊天追加', reply)
        self.assertIn('所选来源', reply)
        self.assertEqual(bridge.brain.last_metrics['operation'], 'desktop_handoff')
        self.assertFalse(dot.called or read.called)

    def test_desktop_handoff_receipt_does_not_claim_original_work_completed(self):
        from test_failure_feedback import FailureFeedbackTests

        _, bridge = self.session()
        list(bridge.brain.clauses('汇报今天的工作', []))

        def interrupt_spoken_notice(*args):
            bridge.session.generation += 1
            return False

        bridge.speak = interrupt_spoken_notice
        with patch.object(bridge.brain.dot, 'clauses') as dot:
            FailureFeedbackTests().run_one(bridge.response, Reply(bridge.generation, '继续第二项'))
        dot.assert_not_called()
        task = bridge.workspace.snapshot()['tasks'][0]
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['stage'], '待桌面接续')
        self.assertEqual(task['provenance']['turn_id'], 'turn-1')
        self.assertIn('没有向原聊天追加请求', task['result'])

    def test_numbered_desktop_handoff_is_bound_to_the_current_snapshot(self):
        voice = VoiceTests().session()
        voice.answer(ReportCommand('report', 'today'), lambda: True)
        reply = voice.answer(ReportCommand('continue', 2), lambda: True)
        self.assertIn('待桌面接续', reply.text)
        self.assertEqual(reply.provenance['thread_id'], SOURCE.source_id)
        self.assertEqual(reply.provenance['turn_id'], 'turn-1')
        self.assertEqual(reply.delivery, ())
        for index in (0, 3):
            self.assertIsNone(
                voice.answer(ReportCommand('continue', index), lambda: True).provenance
            )
        voice.forget()
        self.assertIsNone(voice.answer(ReportCommand('continue', 2), lambda: True).provenance)

    def test_disconnect_during_real_catalog_dispatch_drops_late_report_and_unlocks(self):
        _, bridge = self.session()
        source = bridge.chats.catalog.report

        def read(*args, **kwargs):
            bridge.session.connection = None
            self.assertFalse(kwargs['is_current']())
            return source(*args, **kwargs)

        bridge.chats.catalog.report = read
        with self.assertRaises(Cancelled):
            list(bridge.brain.clauses('汇报今天的工作', []))
        self.assertFalse(bridge.chats.busy)
        self.assertIsNone(bridge.chats.active_request)
        self.assertIsNone(bridge.chats.voice.snapshot)

    def test_delayed_ui_report_does_not_restore_context_after_an_ordinary_question(self):
        helper, bridge = self.session()
        original = bridge.chats.catalog.report

        def read(*args, **kwargs):
            bridge.chats.voice.forget()
            return original(*args, **kwargs)

        bridge.chats.catalog.report = read
        helper.request(bridge, 'report', period='today')
        helper.join(bridge)
        self.assertTrue(bridge.events[-1][1]['ok'])
        self.assertIsNone(bridge.chats.voice.snapshot)

    def test_pending_voice_report_excludes_ui_source_switch_without_stealing_owner(self):
        helper, bridge = self.session()
        entered, release = threading.Event(), threading.Event()
        original = bridge.chats.catalog.report
        errors = []

        def read(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(*args, **kwargs)

        def run():
            try:
                list(bridge.brain.clauses('汇报今天的工作', []))
            except Exception as error:
                errors.append(error)

        with patch.object(bridge.chats.catalog, 'report', side_effect=read):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                helper.request(bridge, 'clear')
                self.assertFalse(bridge.events[-1][1]['ok'])
                self.assertIs(bridge.chats.selection, SOURCE)
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive() or errors)
        self.assertFalse(bridge.chats.busy)

    def test_read_failure_has_fixed_guidance_without_source_exception_text_or_fallback(self):
        from test_failure_feedback import FailureFeedbackTests

        _, bridge = self.session()
        bridge.speak = lambda *args: True
        with patch.object(
            bridge.chats.catalog, 'report', side_effect=DotError('PRIVATE_SOURCE_ERROR')
        ):
            FailureFeedbackTests().run_one(
                bridge.response, Reply(bridge.generation, '汇报今天的工作')
            )
        self.assertNotIn('PRIVATE_SOURCE_ERROR', str(bridge.events) + str(bridge.logs))
        self.assertEqual(bridge.workspace.snapshot()['tasks'][0]['status'], 'failed')
        self.assertIsNone(bridge.chats.voice.snapshot)


if __name__ == '__main__':
    unittest.main()

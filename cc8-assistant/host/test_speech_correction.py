"""Explicit correction drafts through real admission/response owners, without devices."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np

from reply_types import Reply, CorrectedReply
from speech_correction import SpeechCorrection, CorrectionUnavailable, command
import test_failure_feedback as fixtures


class DraftTests(unittest.TestCase):
    def journal(self, text='只读查询沧州今天的天气'):
        clock = [10]
        journal = SpeechCorrection(lambda: clock[0])
        owner = (object(), 'route', 1, 'capture')
        journal.note(text, owner)
        return journal, owner, clock

    def test_explicit_pair_proposes_exactly_one_literal_change(self):
        journal, owner, _ = self.journal()
        draft = journal.prepare('常州，不是沧州', owner)
        self.assertEqual(draft.original, '只读查询沧州今天的天气')
        self.assertEqual(draft.proposed, '只读查询常州今天的天气')
        self.assertEqual(journal.source[0], draft.original)
        self.assertEqual(journal.consume(owner, draft.ident), draft.proposed)
        with self.assertRaises(CorrectionUnavailable):
            journal.consume(owner, draft.ident)

    def test_pair_does_not_depend_on_asr_comma_insertion(self):
        for body in ('常州不是沧州', '常州而不是沧州', '常州，不是沧州'):
            journal, owner, _ = self.journal()
            self.assertEqual(journal.prepare(body, owner).proposed, '只读查询常州今天的天气')

    def test_no_fuzzy_replacement_or_ambiguous_occurrences(self):
        for source, body in (
            ('沧州和沧州', '常州，不是沧州'),
            ('查询沧州', '常州，不是杭州'),
            ('查询沧州', '新' * 65 + '，不是沧州'),
            ('读出150元', '15，不是50'),
            ('读出-3.5度', '7，不是3.5'),
            ('读出负三点五度', '七，不是三点五'),
            ('查询Dotty', 'Codex，不是Dot'),
            ('查询哈哈哈', '呵呵，不是哈哈'),
            ('查询ＣＣ８Plus', '大黄蜂，不是ＣＣ８'),
            ('查询décafé', '茶，不是café'),
            ('查询Cafe\u0301', '茶，不是Cafe'),
            ('查询编号١٢٣', '四，不是٢٣'),
        ):
            journal, owner, _ = self.journal(source)
            with self.assertRaises(CorrectionUnavailable):
                journal.prepare(body, owner)
            self.assertEqual(journal.source[0], source)
            self.assertIsNone(journal.draft)

    def test_whole_multilingual_names_and_numbers_remain_literal(self):
        for original, body, expected in (
            ('查询ＣＣ８的规格', '大黄蜂，不是ＣＣ８', '查询大黄蜂的规格'),
            ('查询Café的营业时间', '茶馆，不是Café', '查询茶馆的营业时间'),
            ('查询编号١٢٣元记录', '四，不是١٢٣', '查询编号四元记录'),
        ):
            with self.subTest(original=original):
                journal, owner, _ = self.journal(original)
                self.assertEqual(journal.prepare(body, owner).proposed, expected)
                self.assertEqual(journal.source[0], original)

    def test_full_replacement_preserves_numbers_and_negative_signs(self):
        journal, owner, _ = self.journal()
        self.assertEqual(
            journal.prepare('查询-3.5摄氏度是否不是正常温度', owner).proposed,
            '查询-3.5摄氏度是否不是正常温度',
        )

    def test_expiry_owner_change_and_old_nonce_never_consume(self):
        for changed in ('time', 'route', 'capture', 'connection', 'id'):
            journal, owner, clock = self.journal()
            draft = journal.prepare('完整新问题', owner)
            current = owner
            ident = draft.ident
            if changed == 'time':
                clock[0] = draft.expires
            if changed == 'route':
                current = (owner[0], 'new', 1, 'capture')
            if changed == 'capture':
                current = (owner[0], 'route', 2, 'new')
            if changed == 'connection':
                current = (object(), *owner[1:])
            if changed == 'id':
                ident = 'b' * 32
            with self.subTest(changed=changed), self.assertRaises(CorrectionUnavailable):
                journal.consume(current, ident)

    def test_overlong_or_invalid_unicode_correction_is_not_an_ordinary_command(self):
        journal, owner, _ = self.journal()
        for body in ('甲' * 513, '\ud800', ''):
            parsed = command('我说的是' + body)
            self.assertEqual(parsed[0], 'draft')
            with self.assertRaises(CorrectionUnavailable):
                journal.prepare(parsed[1], owner)
        for text in ('请解释“我说的是”', '确认更正后删除文件', '取消更正并继续', '不是这个'):
            self.assertIsNone(command(text))
        self.assertEqual(command('Bumblebee，确认更正。'), ('confirm', ''))


class CorrectionFlowTests(unittest.TestCase):
    def session(self):
        brain = fixtures.Brain(receipt=True)
        brain.inputs = []
        original = brain.clauses

        def clauses(text, *args, **kwargs):
            brain.inputs.append(text)
            return original(text, *args, **kwargs)

        brain.clauses = clauses
        b = fixtures.FailureFeedbackTests().session(brain=brain)
        b.session.correction_ui_supported = True
        b.session.capture_id = 'c' * 32
        b.speech_lines = []
        b.speak = lambda text, gen: b.speech_lines.append(text) or True
        return b

    def drain(self, b):
        while not b.response.queue.empty():
            fixtures.FailureFeedbackTests().run_one(b.response, b.response.queue.get_nowait())

    def voice(self, b, text):
        b.cancel('voice')
        b.enqueue_response(Reply(b.generation, text))
        self.drain(b)

    def draft(self, b):
        self.voice(b, '只读查询沧州今天的天气')
        self.voice(b, '我说的是常州，不是沧州')
        return b.response.corrections.draft

    def test_voice_draft_then_confirmation_creates_one_new_request_and_keeps_receipts(self):
        b = self.session()
        self.voice(b, '只读查询沧州今天的天气')
        task = deepcopy(b.workspace.tasks[-1])
        self.voice(b, '我说的是常州，不是沧州')
        self.assertEqual(b.brain.inputs, ['只读查询沧州今天的天气'])
        self.assertEqual(b.workspace.tasks, [task])
        self.assertIsNotNone(b.response.corrections.draft)
        # The assistant never speaks the exact submit command itself.
        self.assertNotIn('确认更正', b.speech_lines[-1])
        self.voice(b, '确认更正')
        self.assertEqual(b.brain.inputs, ['只读查询沧州今天的天气', '只读查询常州今天的天气'])
        self.assertEqual(b.workspace.tasks[0], task)
        self.assertEqual(len(b.workspace.tasks), 2)
        self.assertIsNone(b.session.text_generation)
        self.assertIsNone(b.response.corrections.draft)
        self.voice(b, '确认更正')
        self.assertEqual(len(b.brain.inputs), 2)
        self.assertFalse(any(name == 'transcript' for name, _ in b.events))
        self.assertFalse(any('我说的是' in h['content'] for h in b.response.history))

    def test_cancel_and_inspection_have_no_model_task_or_history_side_effect(self):
        b = self.session()
        self.draft(b)
        tasks, history = deepcopy(b.workspace.tasks), list(b.response.history)
        self.voice(b, '查看更正')
        self.voice(b, '撤回更正')
        self.assertEqual(b.workspace.tasks, tasks)
        self.assertEqual(list(b.response.history), history)
        self.assertEqual(b.brain.calls, 1)
        self.assertIsNone(b.response.corrections.draft)

    def test_expiry_manual_stop_route_and_other_inputs_clear_context(self):
        for change in ('expire', 'stop', 'route', 'new_voice', 'text'):
            b = self.session()
            draft = self.draft(b)
            with self.subTest(change=change):
                if change == 'expire':
                    b.response.corrections.now = lambda: draft.expires
                    b.response.expire_corrections()
                elif change == 'stop':
                    b.stop()
                elif change == 'route':
                    b.change_transport(SimpleNamespace(kind='wifi', route_token='b' * 32))
                elif change == 'new_voice':
                    self.voice(b, '这是另一个完整问题')
                else:
                    b.session.submit_text('明确的桌面新请求')
                    self.drain(b)
                self.assertIsNone(b.response.corrections.draft)
                self.assertFalse(any(t['title'] == draft.proposed for t in b.workspace.tasks))

    def test_ui_nonce_single_use_and_unconfirmed_submission_never_retry(self):
        for fail in (False, True):
            b = self.session()
            draft = self.draft(b)
            data = dict(
                event='speech_correction_action',
                request_id='a' * 32,
                draft_id=draft.ident,
                action='confirm',
            )
            if fail:
                with patch.object(b.session, 'submit_text', side_effect=OSError('fixture')):
                    b.on_event(data)
            else:
                b.on_event(data)
                job = b.response.queue.get_nowait()
                self.assertIsInstance(job, CorrectedReply)
                fixtures.FailureFeedbackTests().run_one(b.response, job)
            b.on_event(data)
            self.assertIsNone(b.response.corrections.draft)
            self.assertEqual(b.brain.calls, 1 if fail else 2)

    def test_invalid_ui_action_does_not_cancel_or_consume_current_draft(self):
        b = self.session()
        draft = self.draft(b)
        generation = b.generation
        for field, value in (('draft_id', 'bad'), ('request_id', True), ('action', 'retry')):
            data = dict(
                event='speech_correction_action',
                request_id='a' * 32,
                draft_id=draft.ident,
                action='confirm',
            )
            data[field] = value
            with self.assertRaises(ValueError):
                b.on_event(data)
        b.on_event(
            dict(
                event='speech_correction_action',
                request_id='b' * 32,
                draft_id='b' * 32,
                action='confirm',
            )
        )
        self.assertIs(b.response.corrections.draft, draft)
        self.assertEqual(b.generation, generation)
        self.assertEqual(b.brain.calls, 1)

    def test_queued_voice_confirmation_or_retraction_cannot_target_a_replacement_draft(self):
        for command_text in ('确认更正', '撤回更正', '查看更正'):
            with self.subTest(command=command_text):
                b = self.session()
                old = self.draft(b)
                b.cancel('voice')
                b.enqueue_response(Reply(b.generation, command_text))
                owner = b.response.hooks.correction_owner()
                source_id = b.response.corrections.source_token(owner)
                b.response.prepare_vocabulary('苏州', '沧州', source_id)
                new = b.response.corrections.draft
                self.assertNotEqual(new.ident, old.ident)
                self.drain(b)
                self.assertIs(b.response.corrections.draft, new)
                self.assertEqual(b.brain.calls, 1)
                self.assertIn('草稿', b.speech_lines[-1])

    def test_queued_confirmation_without_draft_never_consumes_later_draft(self):
        b = self.session()
        self.voice(b, '只读查询沧州今天的天气')
        b.cancel('voice')
        b.enqueue_response(Reply(b.generation, '确认更正'))
        source_id = b.response.corrections.source_token(b.response.hooks.correction_owner())
        b.response.prepare_vocabulary('常州', '沧州', source_id)
        draft = b.response.corrections.draft
        self.drain(b)
        self.assertIs(b.response.corrections.draft, draft)
        self.assertEqual(b.brain.calls, 1)
        self.assertIn('没有对应', b.speech_lines[-1])

    def test_asr_correction_body_cannot_trigger_camera_before_confirmation(self):
        b = self.session()
        b.speech = SimpleNamespace(recognize=lambda _: '我说的是看一看摄像头的画面')
        trace = b.diagnostics.begin('voice')
        fixtures.FailureFeedbackTests().run_one(
            b.recognition, (b.generation, np.ones(16000), False, [], None, None, trace)
        )
        self.assertIsNone(b.visual.generation)
        self.assertEqual(b.response.queue.qsize(), 1)

    def test_legacy_peer_and_stale_queue_cannot_create_draft(self):
        b = self.session()
        b.session.correction_ui_supported = False
        self.voice(b, '原问题')
        self.voice(b, '我说的是新问题')
        self.assertIsNone(b.response.corrections.draft)
        self.assertEqual(b.brain.calls, 1)
        b = self.session()
        b.enqueue_response(Reply(b.generation, '旧问题'))
        b.cancel('session_stop')
        self.drain(b)
        self.assertIsNone(b.response.corrections.source)

    def test_correction_can_replace_a_not_yet_executed_voice_question(self):
        b = self.session()
        b.enqueue_response(Reply(b.generation, '只读查询沧州今天的天气'))
        self.voice(b, '我说的是常州，不是沧州')
        self.assertEqual(b.brain.calls, 0)
        self.voice(b, '确认更正')
        self.assertEqual(b.brain.inputs, ['只读查询常州今天的天气'])

    def test_expired_source_text_is_erased_without_a_draft(self):
        b = self.session()
        self.voice(b, '原问题')
        expiry = b.response.corrections.source[2]
        b.response.corrections.now = lambda: expiry
        b.heartbeat_tick()
        self.assertIsNone(b.response.corrections.source)

    def test_ui_cancel_stops_draft_speech_but_keeps_completed_original_task(self):
        b = self.session()
        draft = self.draft(b)
        task = deepcopy(b.workspace.tasks[-1])
        b.on_event(
            dict(
                event='speech_correction_action',
                request_id='a' * 32,
                draft_id=draft.ident,
                action='cancel',
            )
        )
        self.assertEqual(b.workspace.tasks[-1], task)
        self.assertEqual(b.brain.calls, 1)
        self.assertIsNone(b.response.corrections.draft)

    def test_output_stop_keeps_recent_source_but_discards_pending_draft(self):
        for reason in ('streaming_stop_keyword', 'button', 'asr_stop'):
            b = self.session()
            self.draft(b)
            if reason == 'asr_stop':
                b.speech = SimpleNamespace(recognize=lambda _: '停一下')
                trace = b.diagnostics.begin('voice')
                fixtures.FailureFeedbackTests().run_one(
                    b.recognition, (b.generation, np.ones(16000), False, [], None, None, trace)
                )
            else:
                b.cancel(reason)
            self.assertIsNone(b.response.corrections.draft)
            self.assertEqual(b.response.corrections.source[0], '只读查询沧州今天的天气')
            self.voice(b, '我说的是常州，不是沧州')
            self.assertEqual(b.response.corrections.draft.proposed, '只读查询常州今天的天气')
            cancelled = [fields for name, fields in b.events if name == 'cancel'][-1]
            # The later ordinary-voice cancellation must not discard its draft.
            self.assertNotIn('speech_correction_cleared', cancelled)

    def test_clear_feedback_is_not_sent_before_cancel_cleanup(self):
        b = self.session()
        self.draft(b)
        sends = []
        cancel = b.brain.cancel
        event = b.event

        def cancel_brain():
            sends.append('provider')
            return cancel()

        def capture(name, **fields):
            if name == 'cancel' and fields.get('speech_correction_cleared'):
                sends.append('draft_closed')
            event(name, **fields)

        b.brain.cancel = cancel_brain
        b.event = capture
        b.cancel('button')
        self.assertEqual(sends, ['provider', 'draft_closed'])

    def test_confirm_does_not_cancel_a_newer_task_from_feedback(self):
        b = self.session()
        draft = self.draft(b)
        event = b.event
        admitted = []

        def capture(name, **fields):
            event(name, **fields)
            if name == 'speech_correction' and fields.get('pending') is False and not admitted:
                admitted.append(True)
                b.session.submit_text('明确的更新请求')

        b.event = capture
        b.on_event(
            dict(
                event='speech_correction_action',
                request_id='a' * 32,
                draft_id=draft.ident,
                action='confirm',
            )
        )
        self.drain(b)
        self.assertEqual(b.brain.inputs[-1], '明确的更新请求')
        self.assertEqual(b.brain.calls, 2)

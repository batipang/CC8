"""Receipt-owned narration; no providers, device capture or persisted content."""

from copy import deepcopy
import json
import unittest
from protocol import validate_device_event
from reply_types import Reply, ResultRead
from result_reading import ResultReading, ReadingUnavailable, reading_command
import test_failure_feedback as fixtures

ID = 'a' * 16


class JournalTests(unittest.TestCase):
    def test_before_and_after_result_receipt_have_same_checkpoint(self):
        for early in (False, True):
            journal = ResultReading()
            journal.begin(ID, 2)
            if early:
                journal.original_ack(ID, 2, '第一句。')
            journal.complete(ID, '第一句。第二句。')
            if not early:
                journal.original_ack(ID, 2, '第一句。')
            self.assertEqual(journal.plan(ID).text, '第二句。')

    def test_unmatched_generation_or_unconfirmed_clause_cannot_skip(self):
        journal = ResultReading()
        journal.begin(ID, 2)
        journal.complete(ID, '第一句。第二句。')
        journal.original_ack(ID, 1, '第一句。')
        plan = journal.plan(ID)
        self.assertEqual(plan.offset, 0)
        self.assertFalse(journal.ack(plan, True, '第一句。'))
        self.assertFalse(journal.ack(plan, 0, '第二句。'))
        self.assertEqual(journal.plan(ID).offset, 0)

    def test_unmatched_transcript_requires_explicit_restart(self):
        journal = ResultReading()
        journal.begin(ID, 2)
        journal.original_ack(ID, 2, '不是最后结果。')
        journal.complete(ID, '真实结果。')
        with self.assertRaisesRegex(ReadingUnavailable, '精确对齐'):
            journal.plan(ID)
        self.assertEqual(journal.plan(ID, True).text, '真实结果。')

    def test_restart_fences_old_ack_and_consumes_only_ordered_plan(self):
        journal = ResultReading()
        journal.begin(ID, 2)
        journal.complete(ID, '第一句。第二句。')
        old = journal.plan(ID)
        new = journal.plan(ID, True)
        self.assertFalse(journal.ack(old, 0, '第一句。'))
        self.assertTrue(journal.ack(new, 0, '第一句。'))
        self.assertFalse(journal.ack(new, 0, '第一句。'))
        self.assertTrue(journal.ack(new, 4, '第二句。'))
        with self.assertRaisesRegex(ReadingUnavailable, '读完'):
            journal.plan(ID)

    def test_memory_bounds_eviction_cleaning_and_fresh_process(self):
        journal = ResultReading()
        journal.begin(ID, 2)
        journal.complete(ID, '# 标题\n[来源](https://example.com)\n' + '甲' * 11000)
        plan = journal.plan(ID)
        self.assertTrue(plan.truncated)
        self.assertLessEqual(len(plan.text), 10000)
        self.assertNotIn('https://', plan.text)
        for n in range(12):
            journal.begin(str(n), n)
        self.assertEqual(len(journal.entries), 12)
        with self.assertRaises(ReadingUnavailable):
            journal.plan(ID)
        with self.assertRaises(ReadingUnavailable):
            ResultReading().plan(ID)

    def test_explicit_older_read_target_survives_until_a_new_task(self):
        journal = ResultReading()
        journal.begin('old', 1)
        journal.complete('old', '旧结果。')
        journal.begin('new', 2)
        journal.complete('new', '新结果。')
        tasks = [dict(id='new', status='completed'), dict(id='old', status='completed')]
        journal.plan('old')
        self.assertEqual(journal.target(tasks)['id'], 'old')
        journal.begin('uncertain', 3)
        self.assertIsNone(journal.target([dict(id='uncertain', status='cancelled'), *tasks]))

    def test_only_explicit_narration_phrases_are_local(self):
        self.assertIs(reading_command('大黄蜂，继续播报。'), False)
        self.assertIs(reading_command('从头朗读结果'), True)
        for text in ('继续这个任务', '继续推进', '继续播报然后删除文件', '继续', '看一看'):
            self.assertIsNone(reading_command(text))


class ReadingTests(unittest.TestCase):
    def session(self, text='第一句。第二句。'):
        class Brain:
            calls = 0
            last_metrics = {}

            def cancel(self):
                pass

            def clauses(self, prompt, history, current, update):
                self.calls += 1
                update('dot_result', {'text': text, 'truncated': False})
                yield '第一句。'
                yield '第二句。'

        bridge = fixtures.FailureFeedbackTests().session(brain=Brain())
        return bridge

    def seed(self, bridge, text='第一句。第二句。'):
        ident = bridge.workspace.task_start(bridge.generation, '公开任务')
        bridge.workspace.task_finish(ident, 'completed', text)
        bridge.response.reading.begin(ident, bridge.generation)
        bridge.response.reading.complete(ident, text)
        return ident

    def work(self, bridge):
        job = bridge.response.queue.get_nowait()
        fixtures.FailureFeedbackTests().run_one(bridge.response, job)
        return job

    def request(self, bridge, ident, restart=False, request='b' * 32):
        data = dict(event='result_read_request', request_id=request, id=ident, restart=restart)
        bridge.on_event(data)

    def test_transport_interruption_then_button_continues_without_reexecuting(self):
        bridge = self.session()
        spoken = []

        def original(text, generation):
            spoken.append(text)
            if text == '第一句。':
                bridge.diagnostics.playback_done(generation, confirmed_ack_ms=0)
                return True
            bridge.cancel('transport_switch')
            return False

        bridge.speak = original
        fixtures.FailureFeedbackTests().run_one(
            bridge.response, Reply(bridge.generation, '公开请求')
        )
        task = deepcopy(bridge.workspace.tasks[-1])
        self.assertEqual(task['status'], 'completed')
        spoken.clear()

        def resumed(text, generation):
            spoken.append(text)
            bridge.diagnostics.playback_done(generation, confirmed_ack_ms=0)
            return True

        bridge.speak = resumed
        bridge.session.active = False
        self.request(bridge, task['id'])
        job = self.work(bridge)
        self.assertIsInstance(job, ResultRead)
        self.assertEqual(spoken, ['第二句。'])
        self.assertEqual(bridge.brain.calls, 1)
        self.assertEqual(bridge.workspace.tasks, [task])
        self.assertFalse(bridge.session.active)
        self.assertIsNone(bridge.session.text_generation)
        self.assertEqual(list(bridge.response.history), [])

    def test_speak_success_without_device_ack_does_not_claim_done_or_advance(self):
        bridge = self.session()
        ident = self.seed(bridge)
        bridge.speak = lambda *args: True
        self.request(bridge, ident)
        self.work(bridge)
        self.assertEqual(bridge.response.reading.plan(ident).offset, 0)
        self.assertTrue(
            any(
                name == 'status' and '未确认' in fields.get('message', '')
                for name, fields in bridge.events
            )
        )
        self.assertEqual(bridge.brain.calls, 0)
        self.assertEqual(bridge.workspace.tasks[-1]['status'], 'completed')

    def test_duplicate_unknown_busy_or_noncompleted_never_cancel_current_work(self):
        bridge = self.session()
        ident = self.seed(bridge)
        self.request(bridge, ident)
        generation = bridge.generation
        self.request(bridge, ident)
        self.request(bridge, ident, request='c' * 32)
        self.assertEqual(bridge.generation, generation)
        self.assertEqual(bridge.response.queue.qsize(), 1)
        bridge.session.text_generation = None
        bridge.response.queue.get_nowait()
        bridge.workspace.tasks[-1]['status'] = 'failed'
        self.request(bridge, ident, request='d' * 32)
        self.assertEqual(bridge.generation, generation)
        self.assertTrue(bridge.response.queue.empty())

    def test_full_disconnect_cancels_narration_and_reconnect_never_replays(self):
        bridge = self.session()
        ident = self.seed(bridge, '甲' * 300)
        spoken = []

        def speak(text, generation):
            spoken.append(text)
            bridge.session.connection = None
            bridge.cancel('disconnect')
            return False

        bridge.speak = speak
        self.request(bridge, ident)
        self.work(bridge)
        self.assertEqual(len(spoken), 1)
        bridge.session.connection = object()
        self.assertTrue(bridge.response.queue.empty())
        self.assertEqual(bridge.response.reading.plan(ident).offset, 0)
        self.assertEqual(bridge.brain.calls, 0)

    def test_voice_continue_uses_latest_completed_result_without_new_task(self):
        bridge = self.session()
        ident = self.seed(bridge)
        bridge.response.reading.original_ack(ident, bridge.generation, '第一句。')
        spoken = []

        def speak(text, generation):
            spoken.append(text)
            bridge.diagnostics.playback_done(generation, confirmed_ack_ms=0)
            return True

        bridge.speak = speak
        fixtures.FailureFeedbackTests().run_one(
            bridge.response, Reply(bridge.generation, '继续播报')
        )
        self.assertEqual(spoken, ['第二句。'])
        self.assertEqual(bridge.brain.calls, 0)
        self.assertEqual(len(bridge.workspace.tasks), 1)
        self.assertEqual(list(bridge.response.history), [])
        self.assertNotIn(ident, json.dumps(bridge.diagnostics_report()))

    def test_voice_does_not_substitute_an_older_result_for_unknown_latest_task(self):
        bridge = self.session()
        self.seed(bridge)
        latest = bridge.workspace.task_start(bridge.generation, '后来的任务')
        bridge.workspace.task_finish(latest, 'cancelled')
        bridge.speak = lambda *args: self.fail('Must not read an older result')
        fixtures.FailureFeedbackTests().run_one(
            bridge.response, Reply(bridge.generation, '继续播报')
        )
        self.assertEqual(bridge.brain.calls, 0)
        self.assertEqual(len(bridge.workspace.tasks), 2)
        self.assertTrue(any('最近任务' in fields.get('message', '') for _, fields in bridge.events))

    def test_tts_failure_never_changes_completed_task_or_reexecutes(self):
        bridge = self.session()
        ident = self.seed(bridge)
        before = deepcopy(bridge.workspace.tasks)

        def speak(*args):
            raise TimeoutError('PRIVATE_PROVIDER_BODY')

        bridge.speak = speak
        self.request(bridge, ident)
        self.work(bridge)
        self.assertEqual(bridge.workspace.tasks, before)
        self.assertEqual(bridge.brain.calls, 0)
        self.assertEqual(bridge.response.reading.plan(ident).offset, 0)
        self.assertIsNone(bridge.session.text_generation)
        self.assertNotIn(
            'PRIVATE_PROVIDER_BODY',
            json.dumps([bridge.events, bridge.logs, bridge.diagnostics_report()]),
        )

    def test_queued_explicit_older_target_is_not_lost_to_voice_preemption(self):
        bridge = self.session()
        older = self.seed(bridge, '明确选中的旧结果。')
        self.seed(bridge, '另一份较新的结果。')
        self.request(bridge, older)
        self.assertEqual(
            bridge.response.reading.target(bridge.workspace.snapshot()['tasks'])['id'], older
        )
        bridge.cancel('speech')
        spoken = []

        def speak(text, generation):
            spoken.append(text)
            bridge.diagnostics.playback_done(generation, confirmed_ack_ms=0)
            return True

        bridge.speak = speak
        fixtures.FailureFeedbackTests().run_one(
            bridge.response, Reply(bridge.generation, '继续播报')
        )
        self.assertEqual(spoken, ['明确选中的旧结果。'])
        self.assertEqual(bridge.brain.calls, 0)
        self.assertEqual(len(bridge.workspace.tasks), 2)

    def test_protocol_rejects_text_extra_bad_ids_and_nonboolean_mode(self):
        valid = dict(event='result_read_request', request_id='b' * 32, id=ID, restart=False)
        validate_device_event(valid)
        for updates in (
            {'restart': 1},
            {'restart': 'false'},
            {'text': '替代内容'},
            {'id': '../path'},
            {'request_id': 'x'},
        ):
            with self.assertRaises(ValueError):
                validate_device_event({**valid, **updates})


if __name__ == '__main__':
    unittest.main()

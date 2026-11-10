"""Real submission/worker boundaries; no providers, transports or device capture."""

import json
from pathlib import Path
import queue
import socket
import unittest
from unittest.mock import Mock, patch

from announce import AnnouncementServer, validate_text
from bridge import Bridge
from reply_types import Reply, TextReply
from test_failure_feedback import Brain, SECRET
import test_error_completion_feedback as response_fixture


class TextAdmissionTests(unittest.TestCase):
    def test_rejected_announcement_erases_payload_without_evicting_current_work(self):
        from announce import Announcement

        bridge = self.bridge()
        bridge.submit_text('当前任务')
        old = Announcement(-1, 'PRIVATE', segments=('PRIVATE',), on_segment=lambda n: None)
        bridge.response.enqueue(old)
        self.assertTrue(old.done.is_set())
        self.assertEqual((old.text, old.segments, old.on_segment), ('', (), None))
        self.assertEqual([job.text for job in bridge.response.queue.queue], ['当前任务'])

    def test_stale_response_cannot_evict_a_current_serial_job(self):
        bridge = self.bridge()
        current = bridge.submit_text('当前文档任务')
        bridge.response.enqueue(Reply(current['generation'] - 1, '旧回答一'))
        bridge.response.enqueue(Reply(current['generation'] - 1, '旧回答二'))
        self.assertEqual([job.text for job in bridge.response.queue.queue], ['当前文档任务'])

    def test_reentrant_correction_notice_cannot_enqueue_the_retired_owner(self):
        bridge = self.bridge(active=True)
        generation = bridge.generation
        fired = []

        def publish():
            if not fired:
                fired.append(True)
                bridge.submit_text('后到的新任务')

        with (
            patch.object(bridge.response.corrections, 'note', return_value=True),
            patch.object(bridge.response, 'publish_corrections', publish),
        ):
            bridge.response.enqueue(Reply(generation, '已被替换的旧任务'))
        self.assertEqual([job.text for job in bridge.response.queue.queue], ['后到的新任务'])

    def test_meeting_origin_requires_its_trusted_job_and_cannot_bind_camera_consent(self):
        bridge = self.bridge()
        for kwargs in (
            dict(_job_origin='meeting'),
            dict(
                _job_origin='meeting',
                _job_factory=lambda g: TextReply(g, '文档'),
                _workflow_nonce='a' * 32,
            ),
        ):
            with self.assertRaises(ValueError):
                bridge.session.submit_text('文档', **kwargs)
        with self.assertRaises(ValueError):
            bridge.session.submit_text(
                '文档', _job_origin='meeting', _job_factory=lambda g: TextReply(g, '文档')
            )
        self.assertIsNone(bridge.session.text_generation)
        self.assertTrue(bridge.response.queue.empty())

    def bridge(self, *, active=False):
        with patch('threading.Thread.start'):
            bridge = Bridge(None, Brain())
        bridge.session.connection = object()
        bridge.session.active = active
        bridge.save_status = lambda: None
        bridge.events, bridge.logs = [], []
        bridge.event = lambda name, **fields: bridge.events.append((name, fields))
        bridge.log = lambda name, **fields: bridge.logs.append((name, fields))
        bridge.speak = lambda *_: True
        return bridge

    def work_latest(self, bridge, text):
        jobs = list(bridge.response.queue.queue)
        self.assertEqual([job.text for job in jobs], [text])
        response_fixture.TextFailureTests(methodName='runTest').work(bridge, jobs)
        self.assertEqual(bridge.brain.calls, 1)
        self.assertEqual(bridge.workspace.tasks[-1]['status'], 'completed')
        self.assertIsNone(bridge.session.text_generation)

    def test_text_begin_failure_is_terminal_and_next_request_runs_once(self):
        for active in (False, True):
            with self.subTest(active=active):
                bridge = self.bridge(active=active)
                original = bridge.event
                error = OSError(SECRET)

                def failed(name, **fields):
                    if name == 'text_begin':
                        raise error
                    return original(name, **fields)

                bridge.event = failed
                with self.assertRaises(OSError) as raised:
                    bridge.submit_text('固定公开指令')
                self.assertIs(raised.exception, error)
                self.assertIsNone(bridge.session.text_generation)
                self.assertEqual(bridge.session.active, active)
                self.assertTrue(bridge.response.queue.empty())
                row = bridge.diagnostics_report()['requests'][-1]
                self.assertEqual(row['outcome'], 'failed')
                self.assertEqual(row['error']['stage'], 'transport')
                self.assertEqual(bridge.brain.calls, 0)
                self.assertNotIn(SECRET, json.dumps((bridge.logs, bridge.diagnostics_report())))
                bridge.event = original
                bridge.submit_text('新的公开指令')
                self.work_latest(bridge, '新的公开指令')

    def test_screen_wake_failure_does_not_leave_an_unqueued_text_owner(self):
        bridge = self.bridge()
        bridge.session.screen_sleeping = True
        original = bridge.event

        def failed(name, **fields):
            if name == 'screen_wake':
                raise OSError(SECRET)
            return original(name, **fields)

        bridge.event = failed
        with self.assertRaises(OSError):
            bridge.submit_text('固定公开指令')
        self.assertIsNone(bridge.session.text_generation)
        self.assertTrue(bridge.response.queue.empty())
        self.assertEqual(bridge.diagnostics_report()['requests'][-1]['outcome'], 'failed')

    def test_queue_failure_does_not_leave_frontend_or_owner_waiting(self):
        bridge = self.bridge(active=True)
        error = queue.Full(SECRET)
        with patch.object(bridge, 'enqueue_response', side_effect=error):
            with self.assertRaises(queue.Full) as raised:
                bridge.submit_text('固定公开指令')
        self.assertIs(raised.exception, error)
        self.assertIsNone(bridge.session.text_generation)
        self.assertTrue(bridge.session.active)
        row = bridge.diagnostics_report()['requests'][-1]
        self.assertEqual(row['outcome'], 'failed')
        self.assertEqual(row['error']['stage'], 'runtime')
        self.assertTrue(any(name == 'cancel' for name, _ in bridge.events[1:]))
        bridge.submit_text('新的公开指令')
        self.work_latest(bridge, '新的公开指令')

    def test_secondary_cancel_error_never_masks_the_submission_error(self):
        bridge = self.bridge()
        primary = OSError('primary-' + SECRET)
        bridge.brain.cancel = Mock(side_effect=[None, RuntimeError('secondary-' + SECRET)])
        original = bridge.event

        def failed(name, **fields):
            if name == 'text_begin':
                raise primary
            return original(name, **fields)

        bridge.event = failed
        with self.assertRaises(OSError) as raised:
            bridge.submit_text('固定公开指令')
        self.assertIs(raised.exception, primary)
        self.assertIsNone(bridge.session.text_generation)
        self.assertFalse(bridge.valid(bridge.diagnostics_report()['requests'][-1]['generation']))
        self.assertNotIn(SECRET, json.dumps(bridge.logs))

    def test_nested_replacements_cannot_evict_the_latest_job(self):
        bridge = self.bridge()
        original = bridge.event
        nested = []

        def replace(name, **fields):
            original(name, **fields)
            if name == 'text_begin' and fields['text'] == '旧指令':
                nested.append(bridge.submit_text('中间指令'))
            elif name == 'text_begin' and fields['text'] == '中间指令':
                nested.append(bridge.submit_text('最新指令'))

        bridge.event = replace
        result = bridge.submit_text('旧指令')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(bridge.session.text_generation, bridge.generation)
        self.assertEqual(sum(reply['ok'] is True for reply in nested), 1)
        self.work_latest(bridge, '最新指令')
        rows = bridge.diagnostics_report()['requests']
        self.assertEqual([row['outcome'] for row in rows], ['cancelled', 'cancelled', 'completed'])

    def test_replacement_during_initial_cancel_keeps_the_new_owner(self):
        bridge = self.bridge()
        original = bridge.event
        replaced = []

        def replace(name, **fields):
            original(name, **fields)
            if name == 'cancel' and not replaced:
                replaced.append(True)
                bridge.submit_text('最新指令')

        bridge.event = replace
        result = bridge.submit_text('旧指令')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(bridge.session.text_generation, bridge.generation)
        self.work_latest(bridge, '最新指令')

    def test_failed_old_notification_cannot_cancel_a_new_submission(self):
        bridge = self.bridge()
        original = bridge.event

        def replace(name, **fields):
            original(name, **fields)
            if name == 'text_begin' and fields['text'] == '旧指令':
                bridge.submit_text('最新指令')
                raise OSError(SECRET)

        bridge.event = replace
        try:
            result = bridge.submit_text('旧指令')
        except OSError:
            self.fail('The replaced submission leaked its old delivery error')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'superseded')
        self.work_latest(bridge, '最新指令')

    def test_invalid_unicode_is_rejected_before_cancelling_existing_work(self):
        for value in ('\ud800', '前缀\udfff'):
            for mode in ('text', 'announcement'):
                with self.subTest(mode=mode, value=repr(value)):
                    bridge = self.bridge()
                    before = bridge.generation
                    bridge.session.text_generation = before
                    original = bridge.event

                    def encode(name, **fields):
                        json.dumps(fields, ensure_ascii=False).encode('utf-8')
                        return original(name, **fields)

                    bridge.event = encode
                    with self.assertRaises(ValueError):
                        (bridge.submit_text if mode == 'text' else bridge.announce)(value)
                    self.assertEqual(bridge.generation, before)
                    self.assertEqual(bridge.session.text_generation, before)
                    self.assertEqual(bridge.events, [])
                    self.assertTrue(bridge.response.queue.empty())
                    self.assertEqual(bridge.diagnostics_report()['requests'], [])

    def test_valid_unicode_emoji_and_multiline_text_keep_their_content(self):
        value = json.loads('"\\ud83d\\ude00"') + ' 中文\n第二行\t42'
        self.assertEqual(validate_text(' ' + value + ' '), value)
        bridge = self.bridge()
        result = bridge.submit_text(' ' + value + ' ')
        self.assertTrue(result['ok'])
        self.assertEqual(bridge.response.queue.get_nowait().text, value)
        self.assertFalse(bridge.session.active)

    def test_ambiguous_queue_failure_cannot_execute_the_failed_request(self):
        bridge = self.bridge(active=True)
        original = bridge.enqueue_response
        error = OSError(SECRET)

        def uncertain(job):
            original(job)
            raise error

        with patch.object(bridge, 'enqueue_response', side_effect=uncertain):
            with self.assertRaises(OSError) as raised:
                bridge.submit_text('不得执行的公开测试指令')
        self.assertIs(raised.exception, error)
        old = bridge.response.queue.get_nowait()
        self.assertFalse(bridge.valid(old.generation))
        response_fixture.TextFailureTests(methodName='runTest').work(bridge, [old])
        self.assertEqual(bridge.brain.calls, 0)
        self.assertEqual(bridge.workspace.tasks, [])
        bridge.submit_text('新的公开指令')
        self.work_latest(bridge, '新的公开指令')

    def test_secondary_diagnostic_error_still_clears_the_failed_owner(self):
        bridge = self.bridge()
        primary = OSError(SECRET)
        original = bridge.event

        def failed(name, **fields):
            if name == 'text_begin':
                raise primary
            return original(name, **fields)

        bridge.event = failed
        with patch.object(bridge.diagnostics, 'failure', side_effect=RuntimeError(SECRET)):
            with self.assertRaises(OSError) as raised:
                bridge.submit_text('固定公开指令')
        self.assertIs(raised.exception, primary)
        self.assertIsNone(bridge.session.text_generation)
        self.assertTrue(bridge.response.queue.empty())
        self.assertIsNotNone(bridge.diagnostics_report()['requests'][-1]['outcome'])
        self.assertNotIn(SECRET, json.dumps(bridge.logs))

    def ipc(self, bridge, payload):
        server = AnnouncementServer(bridge, Path('/unused'))
        reader, writer = socket.socketpair()
        with writer:
            writer.settimeout(1)
            self.assertTrue(server.slots.acquire(blocking=False))
            writer.sendall((json.dumps(payload) + '\n').encode())
            server.handle(reader)
            return json.loads(writer.recv(4096))

    def test_real_ipc_rejects_escaped_surrogates_without_changing_owner(self):
        bridge = self.bridge()
        before = bridge.generation
        bridge.session.text_generation = before
        original = bridge.event

        def encode(name, **fields):
            json.dumps(fields, ensure_ascii=False).encode('utf-8')
            return original(name, **fields)

        bridge.event = encode
        self.assertEqual(
            self.ipc(bridge, {'operation': 'text_request', 'text': '\ud800'}),
            {'status': 'invalid_request'},
        )
        self.assertEqual(bridge.generation, before)
        self.assertEqual(bridge.session.text_generation, before)
        self.assertTrue(bridge.response.queue.empty())

    def test_real_ipc_reports_supersession_and_only_executes_latest(self):
        bridge = self.bridge()
        original = bridge.event

        def replace(name, **fields):
            original(name, **fields)
            if name == 'text_begin' and fields['text'] == '旧指令':
                bridge.submit_text('最新指令')

        bridge.event = replace
        result = self.ipc(bridge, {'operation': 'text_request', 'text': '旧指令'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'superseded')
        self.work_latest(bridge, '最新指令')

    def test_health_observes_text_ownership_without_starting_capture(self):
        bridge = self.bridge()
        before = bridge.generation
        self.assertIs(bridge.health().get('text_request_active'), False)
        self.assertEqual(bridge.health().get('session_generation'), before)
        result = bridge.submit_text('固定公开指令')
        current = bridge.health()
        self.assertIs(current['text_request_active'], True)
        self.assertEqual(current['session_generation'], result['generation'])
        self.assertFalse(current['microphone_active'])
        self.assertEqual(bridge.brain.calls, 0)
        self.work_latest(bridge, '固定公开指令')
        after = bridge.health()
        self.assertIs(after['text_request_active'], False)
        self.assertEqual(after['session_generation'], result['generation'])
        self.assertFalse(after['microphone_active'])

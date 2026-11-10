"""Guest boundaries in real composition/workers; no hardware, cloud or owner stores."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np

from announce import Announcement
from brain_router import BrainRouter
from bridge import Bridge
from guest_brain import GuestBrain
from reply_types import (
    Reply,
    VisualReply,
    CorrectedReply,
    WorkflowReply,
    MeetingReply,
    ResultRead,
    BrainPreparation,
)
from report_watch_store import ReportWatchStore
from test_failure_feedback import Brain
import test_failure_feedback as response_fixture
from test_guest_brain import Transport
from test_memory_fixture import isolated_memory


class GuestHostTests(unittest.TestCase):
    def session(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        transport = Transport()
        dot, mini = Brain(receipt=True), Brain(receipt=True)
        router = BrainRouter(mini, dot, guest=GuestBrain(transport.create, 'MiniMax-M3'))
        with patch('threading.Thread.start'):
            b = Bridge(
                None,
                router,
                preference_memory=isolated_memory(),
                report_watch_store=ReportWatchStore(Path(folder.name) / 'watch.json'),
            )
        b.session.connection = object()
        b.session.active = True  # In-memory test session, no AudioRecord or input stream.
        b.save_status = lambda: None
        b.events, b.spoken = [], []
        b.event = lambda kind, **fields: b.events.append((kind, fields))
        b.log = lambda *args, **kwargs: None
        b.speak = lambda text, generation: b.spoken.append(text) or True
        return b, transport, dot

    def enter(self, b):
        b.stop()
        b.reset_identity('guest')
        b.session.active = True

    def voice(self, b, text):
        b.cancel('test_voice')
        b.enqueue_response(Reply(b.generation, text))
        response_fixture.FailureFeedbackTests().run_one(b.response, b.response.queue.get_nowait())

    def test_real_worker_uses_guest_only_workspace_and_keeps_owner_history_private(self):
        b, transport, dot = self.session()
        self.voice(b, 'OWNER_SECRET')
        owner_tasks, owner_history = deepcopy(b.workspace.tasks), deepcopy(b.response.history)
        self.assertTrue(owner_history)
        owner_workspace, owner_reading = b.workspace, b.response.reading
        self.enter(b)
        self.assertFalse(b.owner_access())
        self.assertIsNot(b.workspace, owner_workspace)
        self.assertIsNot(b.response.reading, owner_reading)
        for text in ('继续朗读', '我说的是新的问题', '确认更正', '让 Dot 读取主人文件'):
            self.voice(b, text)
        self.assertEqual(len(transport.calls), 4)
        self.assertNotIn('OWNER_SECRET', json.dumps(transport.calls))
        self.assertEqual(dot.calls, 1)
        self.assertEqual(b.owner_workspace.tasks, owner_tasks)
        self.assertEqual(b.owner_history, owner_history)
        self.assertEqual(list(b.response.history), [])
        self.assertIsNone(b.response.corrections.source)
        self.assertIsNone(b.response.corrections.draft)
        self.assertEqual(len(b.workspace.tasks), 4)
        b.stop()
        b.reset_identity('owner')
        self.assertTrue(b.owner_access())
        self.assertIs(b.workspace, owner_workspace)
        self.assertIs(b.response.reading, owner_reading)
        self.assertEqual(b.response.history, owner_history)
        self.assertEqual(list(b.brain.guest.history), [])

    def test_guest_private_device_events_are_rejected_before_any_handler(self):
        b, _, _ = self.session()
        self.enter(b)
        handler = Mock(side_effect=AssertionError('Owner handler accessed'))
        b.device_events = replace(
            b.device_events,
            preference_action=handler,
            vocabulary_action=handler,
            workflow_action=handler,
            meeting_action=handler,
            read_result=handler,
            correction_action=handler,
            begin_visual=handler,
        )
        packets = [
            {'event': 'dot_chat_request', 'request_id': 'a' * 32, 'action': 'state'},
            {'event': 'saved_task_request', 'request_id': 'a' * 32, 'action': 'list'},
            {'event': 'preference_request', 'request_id': 'a' * 32, 'action': 'list'},
            {'event': 'vocabulary_request', 'request_id': 'a' * 32, 'action': 'list'},
            {
                'event': 'result_read_request',
                'request_id': 'a' * 32,
                'id': 'b' * 16,
                'restart': False,
            },
            {
                'event': 'speech_correction_action',
                'request_id': 'a' * 32,
                'draft_id': 'b' * 32,
                'action': 'confirm',
            },
            {'event': 'vision_request', 'nonce': 'a' * 32, 'mode': 'observe'},
            {
                'event': 'photo_followup_request',
                'nonce': 'a' * 32,
                'photo_id': 'b' * 32,
                'text': '看一看',
            },
            {'event': 'settings', 'brain_provider': 'minimax'},
            {
                'event': 'voice_preferences_set',
                'revision': 0,
                'speech_rate': 'fast',
                'natural_barge': False,
            },
            {'event': 'recognition_language_set', 'revision': 0, 'language': 'en'},
            {'event': 'environment_profile_set', 'revision': 0, 'profile': 'quiet'},
        ]
        before = b.generation
        for packet in packets:
            with self.subTest(event=packet['event']):
                b.on_event(packet)
                self.assertEqual(b.events[-1][0], 'workspace_result')
                self.assertFalse(b.events[-1][1]['ok'])
        handler.assert_not_called()
        self.assertEqual(b.brain.selected, 'dot')
        self.assertEqual(b.generation, before)
        self.assertEqual(b.workspace.tasks, [])
        self.assertEqual(b.session.voice_preferences.revision, 0)

    def test_guest_handshake_does_not_use_owner_hello_or_publish_private_chats(self):
        b, _, _ = self.session()
        self.enter(b)
        b.session.ready = Mock(side_effect=AssertionError('Owner hello'))
        b.chats.publish = Mock(side_effect=AssertionError('Owner chat state'))
        b.on_event({'event': 'device_ready'})
        b.session.ready.assert_not_called()
        b.chats.publish.assert_not_called()

    def test_privileged_queued_jobs_are_rejected_again_at_dispatch(self):
        b, _, _ = self.session()
        self.enter(b)
        b.response.hooks = replace(
            b.response.hooks,
            answer_visual=Mock(side_effect=AssertionError('camera dispatched')),
            run_workflow=Mock(side_effect=AssertionError('workflow dispatched')),
            run_meeting=Mock(side_effect=AssertionError('meeting dispatched')),
            read_announcement=Mock(side_effect=AssertionError('notice dispatched')),
            submit_corrected=Mock(side_effect=AssertionError('correction submitted')),
        )
        for kind in (
            VisualReply,
            CorrectedReply,
            WorkflowReply,
            MeetingReply,
            ResultRead,
            BrainPreparation,
        ):
            job = (
                kind(b.generation)
                if kind is BrainPreparation
                else kind(b.generation, 'OWNER_SECRET')
            )
            b.response.respond_job(job)
        job = Announcement(b.generation, 'OWNER_SECRET')
        b.response.respond_job(job)
        self.assertTrue(job.done.is_set())
        self.assertNotEqual(job.result, 'completed')
        self.assertEqual(b.spoken, [])

    def test_guest_private_text_options_are_rejected_before_cancellation(self):
        b, _, _ = self.session()
        self.enter(b)
        before = b.generation
        for fields in (
            {'corrected': True},
            {'result_id': 'b' * 16},
            {'_job_factory': lambda _: None},
        ):
            with self.assertRaises(RuntimeError):
                b.session.submit_text('OWNER_SECRET', **fields)
        self.assertEqual(b.generation, before)
        self.assertTrue(b.response.queue.empty())
        self.assertEqual(b.workspace.tasks, [])

    def test_guest_reminder_and_watch_and_workflow_ticks_do_not_read_owner_stores(self):
        b, _, _ = self.session()
        self.enter(b)
        b.reminders.hooks = replace(
            b.reminders.hooks, workspace=Mock(side_effect=AssertionError('reminders read'))
        )
        b.chats.watch_ui.scheduler.store.snapshot = Mock(
            side_effect=AssertionError('watch store read')
        )
        b.chats.watch_ui.scheduler.reader.expire_results = Mock(
            side_effect=AssertionError('watch cache read')
        )
        b.workflows.scheduler.store.snapshot = Mock(
            side_effect=AssertionError('workflow store read')
        )
        b.reminders.tick()
        b.chats.watch_ui.scheduler.arm()
        b.chats.watch_ui.scheduler.tick()
        b.workflows.scheduler.arm()
        b.workflows.scheduler.tick()
        with self.assertRaises(ValueError):
            b.reminders.action({'action': 'list'})
        b.reminders.hooks.workspace.assert_not_called()
        b.chats.watch_ui.scheduler.store.snapshot.assert_not_called()
        b.chats.watch_ui.scheduler.reader.expire_results.assert_not_called()
        b.workflows.scheduler.store.snapshot.assert_not_called()
        before = b.generation
        with self.assertRaises(RuntimeError):
            b.announce('OWNER_SECRET')
        self.assertEqual(b.generation, before)

    def test_task_cancel_uses_current_guest_workspace_not_router_original_owner(self):
        b, _, _ = self.session()
        owner_task = b.workspace.task_start(b.generation, 'OWNER_SECRET')
        self.enter(b)
        before = b.generation
        b.on_event({'event': 'task_cancel', 'id': owner_task})
        self.assertEqual(b.generation, before)
        guest_task = b.workspace.task_start(b.generation, '访客问题')
        b.on_event({'event': 'task_cancel', 'id': guest_task})
        self.assertGreater(b.generation, before)

    def test_actual_recognition_worker_routes_camera_words_to_guest_dialogue(self):
        b, transport, dot = self.session()
        self.enter(b)
        b.speech = SimpleNamespace(recognize=lambda _: '帮我看一下眼前有什么')
        b.recognition.hooks = replace(
            b.recognition.hooks,
            photo_voice=Mock(side_effect=AssertionError('Owner photo accessed')),
            begin_visual=Mock(side_effect=AssertionError('Camera opened')),
        )
        job = (b.generation, np.ones(16000, dtype=np.float32) * 0.03)
        with patch.object(b.recognition.queue, 'get', side_effect=[job, SystemExit]):
            with self.assertRaises(SystemExit):
                b.recognition.work()
        b.recognition.hooks.photo_voice.assert_not_called()
        b.recognition.hooks.begin_visual.assert_not_called()
        self.assertFalse(b.response.queue.empty())
        response_fixture.FailureFeedbackTests().run_one(b.response, b.response.queue.get_nowait())
        self.assertEqual(dot.calls, 0)
        self.assertEqual(len(transport.calls), 1)

    def test_private_handler_admission_is_atomic_with_role_exchange(self):
        b, _, _ = self.session()
        entered, release, switched = threading.Event(), threading.Event(), threading.Event()
        observed = []

        def handle(_):
            entered.set()
            if not release.wait(2):
                raise AssertionError('private handler barrier timeout')
            observed.append(b.owner_access())

        def switch():
            b.reset_identity('guest')
            switched.set()

        b.device_events = replace(b.device_events, preference_action=handle)
        reader = threading.Thread(
            target=b.on_event,
            args=({'event': 'preference_request', 'request_id': 'a' * 32, 'action': 'list'},),
            daemon=True,
        )
        changer = threading.Thread(target=switch, daemon=True)
        reader.start()
        self.assertTrue(entered.wait(1))
        changer.start()
        try:
            self.assertFalse(switched.wait(0.03))
        finally:
            release.set()
            reader.join(2)
            changer.join(2)
        self.assertFalse(reader.is_alive())
        self.assertFalse(changer.is_alive())
        self.assertTrue(switched.is_set())
        self.assertEqual(observed, [True])

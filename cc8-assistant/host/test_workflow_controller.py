"""Actual bridge routing, owner cancellation, serial dispatch and private workflow edits."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import uuid

from dot_client import DotError
from protocol import validate_device_event
from reply_types import WorkflowReply
import test_dot_handoff as bridge_fixtures
from test_workflow_run import flow
from test_workflow_store import scheduled
from test_vision import events as camera_events, jpeg
from workflow_definition import builtins
from workflow_store import WorkflowStore


class NativeBrain:
    selected = 'dot'

    def __init__(self):
        self.last_metrics = {}
        self.calls = []

    def cancel(self):
        pass

    def clauses(self, text, history, current, update):
        self.calls.append(text)
        self.last_metrics = dict(provider='dot', turn_id='native-turn-' + str(len(self.calls)))
        update('dot_result', dict(text='实际结果：' + text, truncated=False))
        yield '实际结果：' + text

    def vision_clauses(self, text, image, history, current, update):
        if image != jpeg():
            raise AssertionError('Wrong or retained frame')
        yield from self.clauses(text, history, current, update)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.bridge = b = bridge_fixtures.ControllerTests().session()
        b.workflows.store = WorkflowStore(Path(folder.name) / 'private' / 'workflow.json')
        b.workflows.scheduler.store = b.workflows.store
        b.brain = NativeBrain()
        b.speech = None
        b.spoken = []

        def speak(text, generation):
            b.spoken.append(text)
            b.diagnostics.playback_done(generation, confirmed_ack_ms=0)
            return True

        b.speak = speak
        self.now = 1
        b.workflows.clock = lambda: self.now

    def request(self, action, **fields):
        data = dict(event='workflow_request', request_id=uuid.uuid4().hex, action=action, **fields)
        self.bridge.on_event(data)
        result = next(v for k, v in reversed(self.bridge.events) if k == 'workflow_result')
        self.assertEqual(result['request_id'], data['request_id'])
        return result

    def prepare(self, value=None):
        value = value or flow()
        self.assertTrue(self.request('put', revision=0, value=value, confirmed=True)['ok'])
        result = self.request('prepare', id=value['id'], revision=1)
        self.assertTrue(result['ok'])
        return result['offer']

    def start(self, value=None):
        offer = self.prepare(value)
        result = self.request('run', offer_id=offer['id'], confirmed=True, camera_nonce='')
        self.assertTrue(result['ok'])
        job = self.bridge.response.queue.get_nowait()
        self.assertIsInstance(job, WorkflowReply)
        return job

    def dispatch(self, job):
        try:
            self.bridge.respond_job(job)
        finally:
            self.bridge.finish_text_response(job.generation)

    def test_list_is_passive_and_mutations_require_paused_capture(self):
        b = self.bridge
        self.assertTrue(self.request('list')['ok'])
        self.assertFalse(b.workflows.store.path.parent.exists())
        b.session.active = True
        self.assertFalse(self.request('reset', revision=0, confirmed=True)['ok'])
        self.assertFalse(b.workflows.store.path.parent.exists())
        self.assertEqual(b.brain.calls, [])

    def test_real_serial_dispatch_commits_results_and_never_starts_microphone(self):
        b = self.bridge
        job = self.start()
        self.assertEqual(b.brain.calls, [])
        self.dispatch(job)
        self.assertEqual(b.brain.calls, ['任务1', '任务2'])
        self.assertEqual(b.workflows.active.state, 'completed')
        self.assertEqual(b.workspace.snapshot()['tasks'][-1]['status'], 'completed')
        self.assertIn('实际结果：任务2', ''.join(b.spoken))
        self.assertFalse(b.session.active)
        self.assertIsNone(b.session.text_generation)
        result = self.request('result', run_id=job.run.ident, step_id=flow()['steps'][0]['id'])
        self.assertEqual(result['result']['result']['text'], '实际结果：任务1')
        self.assertNotIn('实际结果：', b.workflows.store.path.read_text())
        self.assertEqual(list(b.response.history), [])

    def test_offer_is_once_only_and_expires_or_route_generation_change_revokes_it(self):
        offer = self.prepare()
        self.now = 92
        self.assertFalse(
            self.request('run', offer_id=offer['id'], confirmed=True, camera_nonce='')['ok']
        )
        self.assertTrue(self.bridge.response.queue.empty())
        offer = self.request('prepare', id=flow()['id'], revision=1)['offer']
        self.bridge.cancel('button')
        self.assertFalse(
            self.request('run', offer_id=offer['id'], confirmed=True, camera_nonce='')['ok']
        )
        self.assertEqual(self.bridge.brain.calls, [])

    def test_double_run_request_never_replays_and_stop_before_queue_has_no_effect(self):
        offer = self.prepare()
        data = dict(
            event='workflow_request',
            request_id=uuid.uuid4().hex,
            action='run',
            offer_id=offer['id'],
            confirmed=True,
            camera_nonce='',
        )
        self.bridge.on_event(data)
        job = self.bridge.response.queue.get_nowait()
        self.bridge.on_event(data)
        self.bridge.cancel('button')
        self.dispatch(job)
        self.assertTrue(self.bridge.response.queue.empty())
        self.assertEqual(self.bridge.brain.calls, [])
        self.assertEqual(job.run.state, 'cancelled')

    def test_explicit_cancel_acknowledges_its_successor_and_keeps_queued_work_unexecuted(self):
        job = self.start()
        result = self.request('cancel', run_id=job.run.ident)
        self.assertTrue(result['ok'])
        self.assertEqual(result['state']['run']['state'], 'cancelled')
        self.dispatch(job)
        self.assertEqual(self.bridge.brain.calls, [])

    def test_stop_narration_preserves_completed_work_and_no_step_is_replayed(self):
        b = self.bridge
        job = self.start()

        def stop(text, generation):
            b.cancel('button')
            return False

        b.speak = stop
        self.dispatch(job)
        self.assertEqual(job.run.state, 'completed')
        self.assertEqual(b.workspace.snapshot()['tasks'][-1]['status'], 'completed')
        self.assertEqual(len(b.brain.calls), 2)

    def test_last_receipt_parent_commit_cannot_lose_a_race_to_stop_callback(self):
        b = self.bridge
        job = self.start(flow(1))
        normal = b.publish_workspace
        stopped = []

        def publish():
            normal()
            if b.workflows.active.state == 'completed' and not stopped:
                stopped.append(True)
                b.cancel('button')

        b.publish_workspace = publish
        self.dispatch(job)
        self.assertEqual(job.run.state, 'completed')
        self.assertEqual(b.workspace.snapshot()['tasks'][-1]['status'], 'completed')
        self.assertEqual(b.spoken, [])

    def test_middle_failure_keeps_completed_step_and_does_not_run_third(self):
        b = self.bridge
        original = b.brain.clauses

        def fail_second(text, *args):
            if text == '任务2':
                raise DotError('synthetic failure')
            yield from original(text, *args)

        b.brain.clauses = fail_second
        job = self.start(flow(3))
        self.dispatch(job)
        self.assertEqual(job.run.state, 'failed')
        self.assertEqual(
            [s['state'] for s in job.run.snapshot()['steps']],
            ['completed', 'unconfirmed', 'pending'],
        )
        self.assertEqual(b.brain.calls, ['任务1'])
        self.assertEqual(b.spoken, [])

    def test_camera_nonce_is_explicit_and_prepare_never_opens_camera(self):
        b = self.bridge
        offer = self.prepare(builtins()[2])
        self.assertTrue(offer['camera_required'])
        self.assertIsNone(b.visual.generation)
        self.assertFalse(
            self.request('run', offer_id=offer['id'], confirmed=True, camera_nonce='')['ok']
        )
        self.assertEqual(b.brain.calls, [])
        self.assertTrue(b.response.queue.empty())

    def test_get_and_result_stay_bounded_and_new_connection_cannot_use_old_offer(self):
        offer = self.prepare()
        b = self.bridge
        b.session.connection = object()
        self.assertFalse(
            self.request('run', offer_id=offer['id'], confirmed=True, camera_nonce='')['ok']
        )
        self.assertEqual(b.brain.calls, [])

    def test_strict_protocol_rejects_hidden_payload_or_missing_consent_before_side_effects(self):
        valid = dict(
            event='workflow_request',
            request_id='a' * 32,
            action='run',
            offer_id='b' * 32,
            confirmed=True,
            camera_nonce='',
        )
        for changed in (
            dict(confirmed=1),
            dict(confirmed=False),
            dict(camera_nonce=True),
            dict(text='隐藏命令'),
            dict(action=[]),
        ):
            with self.assertRaises(ValueError):
                validate_device_event(dict(valid, **changed))
        self.assertEqual(self.bridge.brain.calls, [])
        self.assertFalse(self.bridge.workflows.store.path.exists())

    def schedule(self):
        b = self.bridge
        self.now = 100
        b.workflows.store.clock = lambda: self.now
        b.workflows.scheduler.clock = lambda: self.now
        b.brain.minimax = SimpleNamespace(
            transport=SimpleNamespace(
                post_json=lambda route, body, current: {
                    'base_resp': {'status_code': 0},
                    'organic': [],
                }
            )
        )
        result = self.request('put', revision=0, value=scheduled(), confirmed=True)
        self.assertTrue(result['ok'])
        self.now = 400
        b.workflows.scheduler.tick()
        return b.response.queue.get_nowait()

    def test_scheduled_execution_and_actual_playback_receipt_settle_separately(self):
        job = self.schedule()
        b = self.bridge
        self.assertEqual(b.workflows.store.snapshot()['attempts'][0]['execution'], 'claimed')
        self.dispatch(job)
        row = b.workflows.store.snapshot()['attempts'][0]
        self.assertEqual((row['execution'], row['delivery']), ('completed', 'completed'))
        b.workflows.scheduler.tick()
        self.assertTrue(b.response.queue.empty())
        self.assertFalse(b.session.active)

    def test_scheduled_completion_keeps_actual_open_microphone_state(self):
        job = self.schedule()
        b = self.bridge
        b.session.active = True  # Synthetic state only; no capture is started.
        self.dispatch(job)
        statuses = [v for k, v in b.events if k == 'status']
        self.assertEqual(statuses[-1]['phase'], 'listening')
        self.assertIn('我在听', statuses[-1]['message'])
        self.assertTrue(b.session.active)

    def test_tts_without_matched_device_ack_does_not_claim_delivery_or_replay_work(self):
        job = self.schedule()
        b = self.bridge
        b.speak = lambda *args: True
        self.dispatch(job)
        row = b.workflows.store.snapshot()['attempts'][0]
        self.assertEqual((row['execution'], row['delivery']), ('completed', 'unconfirmed'))
        b.workflows.scheduler.tick()
        self.assertTrue(b.response.queue.empty())
        self.assertEqual(b.workspace.snapshot()['tasks'][-1]['status'], 'completed')

    def test_maintenance_after_queue_admission_revokes_scheduled_operation(self):
        job = self.schedule()
        b = self.bridge
        b.workflows.scheduler.hooks = replace(b.workflows.scheduler.hooks, blocked=lambda: True)
        self.dispatch(job)
        self.assertEqual(b.spoken, [])
        self.assertEqual(job.run.state, 'cancelled')
        self.assertEqual(b.workflows.store.snapshot()['attempts'][0]['execution'], 'cancelled')

    def test_fresh_camera_confirmation_uses_existing_inbox_once_and_discards_frame(self):
        b = self.bridge
        offer = self.prepare(builtins()[2])
        original = b.event

        def event(name, **fields):
            original(name, **fields)
            if name == 'camera_capture':
                self.assertEqual(fields['nonce'], 'c' * 32)
                self.assertEqual(fields['origin'], 'button')
                self.assertFalse(fields['followup_allowed'])
                for data in camera_events(b.visual.inbox.pending):
                    b.on_event(data)

        b.event = event
        self.assertTrue(
            self.request('run', offer_id=offer['id'], confirmed=True, camera_nonce='c' * 32)['ok']
        )
        job = b.response.queue.get_nowait()
        self.dispatch(job)
        self.assertEqual(job.run.state, 'completed')
        self.assertEqual(len(b.brain.calls), 1)
        self.assertIsNone(b.visual.inbox.pending)
        self.assertIsNone(b.visual.generation)
        self.assertEqual(b.metrics.get('desktop_snapshots_captured', 0), 0)

    def test_camera_error_cannot_publish_a_completed_frame_or_run_vision_model(self):
        b = self.bridge
        offer = self.prepare(builtins()[2])
        original = b.event

        def event(name, **fields):
            original(name, **fields)
            if name == 'camera_capture':
                capture = b.visual.inbox.pending
                b.on_event(
                    dict(
                        event='camera_error',
                        request_id=capture.ident,
                        vision_generation=capture.generation,
                        error='camera_unavailable',
                    )
                )

        b.event = event
        self.request('run', offer_id=offer['id'], confirmed=True, camera_nonce='c' * 32)
        self.dispatch(b.response.queue.get_nowait())
        self.assertEqual(b.workflows.active.state, 'failed')
        self.assertEqual(b.brain.calls, [])
        done = [v for k, v in b.events if k == 'vision_done']
        self.assertEqual([v['result'] for v in done], ['failed'])
        self.assertIsNone(b.visual.inbox.pending)

"""Real bridge + framed device events with injected providers; no capture or cloud."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from brain_router import BrainRouter
from bridge import Bridge
from guest_brain import GuestBrain
from identity_mode import IdentityModeStore, ModeState
from minimax_client import Cancelled
from protocol import Packet, SPEAKER, MIC, EVENT, receive
from test_guest_brain import Transport
from test_failure_feedback import Brain
from test_memory_fixture import isolated_memory
from test_runtime import Fragmented
from workflow_run import WorkflowRun
from test_workflow_run import flow, receipt


class Channel:
    kind = 'usb'
    application_ready = True

    def __init__(self):
        self.route_token = 'b' * 32
        self.events = []

    def sendall(self, data):
        packet = receive(Fragmented(data))
        if packet.kind != EVENT:
            raise AssertionError('Unexpected audio I/O')
        import json

        self.events.append(json.loads(packet.data))


class IdentityRuntimeTests(unittest.TestCase):
    def session(self, *, saved_guest=False, speech=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = IdentityModeStore(Path(folder.name) / 'private/identity-mode.json')
        if saved_guest:
            store.change(ModeState(), 'guest')
        channel, transport = Channel(), Transport()
        router = BrainRouter(Brain(), Brain(), guest=GuestBrain(transport.create, 'MiniMax-M3'))
        with patch('threading.Thread.start'):
            b = Bridge(speech, router, identity_store=store, preference_memory=isolated_memory())
        b.session.connection = channel
        b.session.bound_route_token = channel.route_token
        b.session.transport_kind = channel.kind
        b.save_status = lambda: None
        b.log = lambda *args, **kwargs: None
        b.chats.publish = Mock()
        return b, channel

    def ready(self, b, **fields):
        b.on_event(
            {
                'event': 'device_ready',
                'identity_version': 1,
                'capture_active': False,
                'capture_reject_version': 1,
                'capture_start_ack_version': 1,
                'paired_capture_version': 1,
                'speech_correction_version': 1,
                **fields,
            }
        )

    def ack(self, b):
        ticket = b.identity.application
        with patch('device_session.window_available', return_value=False):
            b.on_event(
                {
                    'event': 'identity_applied',
                    'token': ticket.token,
                    'mode': ticket.state.mode,
                    'revision': ticket.state.revision,
                }
            )

    def test_no_private_hello_or_capture_or_pcm_until_exact_native_ack(self):
        b, channel = self.session()
        self.assertFalse(b.owner_access())
        self.ready(b)
        self.assertEqual([e['event'] for e in channel.events], ['identity_state'])
        b.capture.on_packet = Mock(side_effect=AssertionError('Unconfirmed mic accepted'))
        b.on_packet(Packet(MIC, data=b'\0\0' * 320))
        b.capture.on_packet.assert_not_called()
        with self.assertRaises(Cancelled):
            b.send(Packet(SPEAKER, data=b'\0\0' * 320))
        with self.assertRaises(Cancelled):
            b.send(Packet(SPEAKER, data=b'\0\0' * 320).bind_route(channel.route_token))
        with self.assertRaises(RuntimeError):
            b.submit_text('未确认时不要提交')
        self.ack(b)
        self.assertTrue(b.owner_access())
        self.assertTrue(any(e['event'] == 'hello' for e in channel.events))
        b.chats.publish.assert_called_once_with(ok=True)
        before = len(channel.events)
        self.ack(b)
        self.assertEqual(len(channel.events), before)
        self.assertFalse(b.session.active)

    def test_actual_transport_change_revokes_old_peer_capability_before_new_ready(self):
        b, _ = self.session(saved_guest=True)
        self.ready(b)
        self.ack(b)
        old = b.identity.application
        next_channel = Channel()
        next_channel.route_token = 'd' * 32
        b.change_transport(next_channel)
        self.assertFalse(b.dialogue_access())
        self.assertFalse(b.identity_runtime.peer_supported)
        self.assertIsNone(b.identity_runtime.last_ready)
        self.assertFalse(
            b.identity.applied(
                {'event': 'identity_applied', 'token': old.token, 'mode': 'guest', 'revision': 1}
            )
        )
        self.ready(b)
        self.ack(b)
        self.assertTrue(b.dialogue_access())
        self.assertFalse(b.session.active)

    def test_guest_request_closes_owner_output_before_stop_and_sends_minimal_hello(self):
        b, channel = self.session()
        self.ready(b)
        self.ack(b)
        b.workspace.task_start(b.generation, 'OWNER_SECRET')
        channel.events.clear()
        b.on_event(
            {
                'event': 'identity_request',
                'request_id': 'a' * 32,
                'arguments': {'action': 'guest', 'revision': 0},
            }
        )
        self.assertEqual(b.identity.state, ModeState('guest', 1))
        self.assertFalse(b.dialogue_access())
        self.assertNotIn('OWNER_SECRET', str(channel.events))
        self.ack(b)
        self.assertTrue(b.dialogue_access())
        self.assertFalse(b.owner_access())
        hello = [e for e in channel.events if e['event'] == 'hello'][-1]
        self.assertFalse(hello['supports_dot_chat_handoff'])
        self.assertFalse(hello['supports_meetings'])
        self.assertFalse(hello['supports_photo_followup'])
        self.assertEqual(hello['meeting_state'], {})
        self.assertEqual(hello['identity_mode'], 'guest')
        self.assertEqual(hello['voice_preferences']['recognition_language'], 'auto')
        self.assertFalse(hello['voice_preferences']['supported'])
        self.assertFalse(hello['voice_preferences']['recognition_language_supported'])
        self.assertEqual(b.workspace.tasks, [])
        self.assertFalse(b.session.active)

    def test_saved_guest_and_unsupported_peer_cannot_receive_owner_or_start_work(self):
        b, channel = self.session(saved_guest=True)
        b.on_event({'event': 'device_ready'})
        self.assertEqual([e['event'] for e in channel.events], ['identity_unavailable'])
        self.assertFalse(b.dialogue_access())
        self.assertFalse(b.owner_access())
        b.event('dot_chat_state', private='OWNER_SECRET')
        b.event('dot_result', text='OWNER_SECRET')
        self.assertNotIn('OWNER_SECRET', str(channel.events))
        b.chats.publish.assert_not_called()
        self.ready(b, identity_guest_requested=True)
        self.ack(b)
        self.assertTrue(b.identity.guest)
        self.assertTrue(b.dialogue_access())

    def test_native_persisted_guest_choice_cannot_be_overridden_by_default_owner(self):
        b, channel = self.session()
        b.owner_workspace.task_start(100, 'OWNER_SECRET')
        self.ready(b, identity_guest_requested=True)
        self.assertEqual(b.identity.state.mode, 'guest')
        self.assertNotIn('OWNER_SECRET', str(channel.events))
        self.assertFalse(any(e['event'] == 'hello' for e in channel.events))
        self.ack(b)
        self.assertTrue(b.identity.guest)
        self.assertFalse(b.owner_access())

    def test_guest_event_gate_drops_private_payloads_and_adds_role_revision(self):
        b, channel = self.session(saved_guest=True)
        self.ready(b)
        self.ack(b)
        before = len(channel.events)
        for name in (
            'dot_chat_state',
            'meeting_result',
            'preference_result',
            'saved_task_result',
            'camera_capture',
            'lan_config',
        ):
            b.event(name, value='OWNER_SECRET')
        b.event('web_sources', items=[{'title': 'OWNER_SECRET'}])
        self.assertEqual(len(channel.events), before)
        b.event('transcript', text='访客问题')
        self.assertEqual(channel.events[-1]['identity_mode'], 'guest')
        self.assertEqual(channel.events[-1]['identity_revision'], 1)
        self.assertEqual(channel.events[-1]['text'], '访客问题')

    def test_route_handover_invalidates_access_and_old_ack_without_automatic_capture(self):
        b, channel = self.session(saved_guest=True)
        self.ready(b)
        old = b.identity.application
        self.ack(b)
        b.session.bound_route_token = 'c' * 32
        channel.route_token = 'c' * 32
        self.assertFalse(b.dialogue_access())
        self.ready(b, identity_guest_requested=True)
        b.on_event(
            {
                'event': 'identity_applied',
                'token': old.token,
                'mode': old.state.mode,
                'revision': old.state.revision,
            }
        )
        self.assertFalse(b.dialogue_access())
        self.ack(b)
        self.assertTrue(b.dialogue_access())
        self.assertFalse(b.session.active)

    def test_owner_restore_waits_for_actual_native_ack_before_access_is_reopened(self):
        b, channel = self.session(saved_guest=True)
        self.ready(b)
        self.ack(b)
        results = []
        sent = threading.Event()
        original = channel.sendall

        def send(data):
            original(data)
            if channel.events[-1]['event'] == 'identity_state':
                sent.set()

        channel.sendall = send
        worker = threading.Thread(
            target=lambda: results.append(
                b.identity.owner_from_desktop({'revision': 1}, timeout=2)
            ),
            daemon=True,
        )
        worker.start()
        self.assertTrue(sent.wait(1))
        self.assertFalse(b.owner_access())
        self.ack(b)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]['ok'])
        self.assertTrue(b.owner_access())
        self.assertFalse(b.session.active)

    def test_guest_uses_default_voice_preferences_and_owner_restore_recovers_originals(self):
        rates, languages = [], []
        speech = SimpleNamespace(
            set_voice_rate=rates.append,
            recognition_language_supported=True,
            set_recognition_language=languages.append,
            recognizer=SimpleNamespace(engine='minimax'),
        )
        b, channel = self.session(speech=speech)
        self.ready(b)
        self.ack(b)
        b.on_event(
            {
                'event': 'voice_preferences_set',
                'revision': 0,
                'speech_rate': 'slow',
                'natural_barge': False,
            }
        )
        b.on_event({'event': 'recognition_language_set', 'revision': 1, 'language': 'en'})
        b.on_event({'event': 'environment_profile_set', 'revision': 2, 'profile': 'quiet'})
        original = b.session.voice_preferences_state()
        b.on_event(
            {
                'event': 'identity_request',
                'request_id': 'a' * 32,
                'arguments': {'action': 'guest', 'revision': 0},
            }
        )
        self.ack(b)
        guest = b.session.voice_preferences_state()
        self.assertEqual(
            (
                guest['speech_rate'],
                guest['recognition_language'],
                guest['natural_barge'],
                guest['environment_profile'],
            ),
            ('normal', 'auto', True, 'normal'),
        )
        self.assertEqual((rates[-1], languages[-1]), ('normal', 'auto'))
        b.identity.owner_from_desktop({'revision': 1}, timeout=0)
        self.ack(b)
        self.assertEqual(b.session.voice_preferences_state(), original)
        self.assertEqual((rates[-1], languages[-1]), ('slow', 'en'))

    def test_malformed_or_busy_guest_request_does_not_mutate_or_stop_owner_work(self):
        b, _ = self.session()
        self.ready(b)
        self.ack(b)
        token = b.identity.application.token
        before = b.generation
        for extra in ({'request_id': 'bad'}, {'actor': 'guest'}):
            b.on_event(
                {
                    'event': 'identity_request',
                    'request_id': 'a' * 32,
                    'arguments': {'action': 'guest', 'revision': 0},
                    **extra,
                }
            )
            self.assertTrue(b.owner_access())
        b.response.busy = True
        b.on_event(
            {
                'event': 'identity_request',
                'request_id': 'a' * 32,
                'arguments': {'action': 'guest', 'revision': 0},
            }
        )
        self.assertTrue(b.owner_access())
        self.assertEqual(b.generation, before)
        self.assertEqual(b.identity.application.token, token)
        self.assertFalse(b.identity.store.path.exists())

    def test_finished_workflow_receipt_does_not_permanently_block_guest_switch(self):
        b, _ = self.session()
        self.ready(b)
        self.ack(b)
        run = WorkflowRun(flow(1))
        b.workflows.active = run
        self.assertTrue(b.identity_runtime.busy())
        result = run.run(lambda *args: receipt(), current=lambda: True)
        self.assertEqual(result['state'], 'completed')
        self.assertFalse(b.identity_runtime.busy())
        b.on_event(
            {
                'event': 'identity_request',
                'request_id': 'a' * 32,
                'arguments': {'action': 'guest', 'revision': 0},
            }
        )
        self.assertEqual(b.identity.state.mode, 'guest')
        self.assertEqual(run.state, 'completed')

    def test_duplicate_device_ready_does_not_revoke_active_role_or_owner_work(self):
        b, channel = self.session()
        self.ready(b)
        self.ack(b)
        token, before = b.identity.application.token, len(channel.events)
        b.response.busy = True
        self.ready(b)
        self.assertTrue(b.owner_access())
        self.assertEqual(b.identity.application.token, token)
        self.assertEqual(len(channel.events), before)

"""Actual Bridge routing/admission and real meeting worker, without device/cloud I/O."""

from dataclasses import replace
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from protocol import MEETING, Packet, receive
import test_dot_handoff as bridge_fixtures
from test_meeting_worker import Client
from test_protocol_fuzz import Fragmented


class MeetingControllerTests(unittest.TestCase):
    def setUp(self):
        self.bridge = b = bridge_fixtures.ControllerTests().session()
        b.session.connection = SimpleNamespace(
            kind='usb', route_token='c' * 32, application_ready=True
        )
        b.session.bound_route_token = 'c' * 32
        self.now = 1
        b.meetings.clock = lambda: self.now
        self.clients = []

        def factory():
            client = Client()
            self.clients.append(client)
            return client

        b.meetings.worker.factory = factory
        self.peer()

        def cleanup():
            b.meetings.close()
            if b.meetings.worker.thread is not None:
                b.meetings.worker.thread.join(2)
                self.assertFalse(b.meetings.worker.thread.is_alive())

        self.addCleanup(cleanup)

    def peer(self, version=1):
        self.bridge.on_packet(
            Packet.event(
                'device_ready',
                meeting_version=version,
                capture_reject_version=1,
                capture_start_ack_version=1,
            ).bind_route(self.bridge.session.bound_route_token)
        )

    def request(self, action, **fields):
        data = dict(event='meeting_request', request_id=uuid.uuid4().hex, action=action, **fields)
        self.bridge.on_packet(
            Packet.event(**data).bind_route(self.bridge.session.bound_route_token)
        )
        return next(v for k, v in reversed(self.bridge.events) if k == 'meeting_result')

    def start(self):
        prepared = self.request('prepare')
        self.assertTrue(prepared['ok'], prepared)
        value = self.request(
            'start', offer_id=prepared['offer']['id'], confirmed=True, save_audio=False
        )
        self.assertTrue(value['ok'], value)
        return value['capture']

    def audio(self, capture, *, sequence=0, frame=0, token=None, nonce=None):
        packet = Packet(
            MEETING,
            capture['generation'],
            sequence,
            frame,
            1,
            bytes.fromhex(nonce or capture['nonce']) + b'\x00\x20' * 1600,
        )
        self.bridge.on_packet(packet.bind_route(token or self.bridge.session.bound_route_token))

    def idle(self):
        deadline = time.monotonic() + 2
        while self.bridge.meetings.worker.busy() and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        self.assertFalse(self.bridge.meetings.worker.busy())

    def test_status_and_prepare_are_passive_and_only_explicit_confirmed_start_creates_worker(self):
        b = self.bridge
        self.assertTrue(self.request('status')['ok'])
        offer = self.request('prepare')['offer']
        self.assertFalse(offer['raw_audio_supported'])
        self.assertEqual(offer['audio_destination'], 'MiniMax')
        self.assertIsNone(b.meetings.model)
        self.assertIsNone(b.meetings.worker.thread)
        self.assertEqual(self.clients, [])
        hello = next(v for k, v in b.events if k == 'hello')
        self.assertTrue(hello['supports_meetings'])

    def test_whole_route_bound_capture_pause_and_end_uses_real_worker_but_not_normal_asr(self):
        b = self.bridge
        capture = self.start()
        self.audio(capture)
        self.assertEqual(self.clients, [])
        self.assertTrue(self.request('pause', id=capture['id'])['ok'])
        self.idle()
        self.assertEqual(b.meetings.model.rows[0]['text'], '真实回执')
        self.assertFalse(b.session.active)
        self.assertTrue(b.recognition.queue.empty())
        result = self.request('end', id=capture['id'])
        self.assertTrue(result['state']['processing_complete'])
        self.assertFalse(b.meetings.reserved())
        self.assertEqual(self.clients[0].calls, 1)

    def test_health_reports_meeting_capture_lease_without_text_nonce_or_meeting_identity(self):
        capture = self.start()
        health = self.bridge.health()
        self.assertTrue(health['microphone_active'])
        self.assertTrue(health['meeting_capture_requested'])
        self.assertEqual(health['meeting_mode'], 'capturing')
        self.assertNotIn(capture['nonce'], repr(health))
        self.assertNotIn(capture['id'], repr(health))
        self.request('pause', id=capture['id'])
        self.assertFalse(self.bridge.health()['microphone_active'])
        self.assertEqual(self.bridge.health()['meeting_mode'], 'paused')

    def test_normal_voice_text_announcement_camera_reminder_and_workflow_blocked_during_meeting(
        self,
    ):
        b = self.bridge
        capture = self.start()
        generation = b.generation
        with self.assertRaises(RuntimeError):
            b.submit_text('不要打断会议')
        with self.assertRaises(RuntimeError):
            b.announce('不要打断会议')
        with self.assertRaises(RuntimeError):
            b.visual.request_snapshot()
        b.begin_visual('查看', 'button', 'a' * 32)
        self.assertEqual(b.generation, generation)
        self.assertTrue(b.reminders.hooks.can_deliver() is False)
        self.assertFalse(b.workflows.scheduler.hooks.ready())
        with patch.object(
            b.capture.echo, 'open', side_effect=AssertionError('Ordinary AEC opened')
        ):
            b.on_event(
                dict(
                    event='session_start',
                    epoch=8,
                    capture_id='e' * 32,
                    capture_start_token=b.session.capture_start_token,
                )
            )
        self.assertFalse(b.session.active)
        self.assertEqual(b.meetings.model.state, 'capturing')
        self.assertEqual(b.meetings.model.ident, capture['id'])
        self.assertIn('capture_rejected', [k for k, _ in b.events])

    def test_expired_consumed_or_old_generation_offer_never_starts_capture(self):
        for mode in ('expired', 'generation', 'reused'):
            with self.subTest(mode=mode):
                offer = self.request('prepare')['offer']
                if mode == 'expired':
                    self.now += 31
                elif mode == 'generation':
                    self.bridge.cancel('button')
                args = dict(offer_id=offer['id'], confirmed=True, save_audio=False)
                self.assertFalse(
                    self.request('start', **args)['ok']
                    if mode != 'reused'
                    else self.request(
                        'start', offer_id=offer['id'], confirmed=True, save_audio=True
                    )['ok']
                )
                self.assertFalse(self.request('start', **args)['ok'])
                self.assertIsNone(self.bridge.meetings.worker.thread)

    def test_replayed_start_id_does_not_reopen_capture(self):
        offer = self.request('prepare')['offer']
        request = dict(
            event='meeting_request',
            request_id='f' * 32,
            action='start',
            offer_id=offer['id'],
            confirmed=True,
            save_audio=False,
        )
        self.bridge.on_event(request)
        capture = self.bridge.meetings.model.owner
        self.bridge.on_event(request)
        self.assertEqual(self.bridge.meetings.model.owner, capture)
        self.assertFalse(self.bridge.events[-1][1]['ok'])

    def test_pause_keeps_reservation_and_resume_needs_fresh_offer_and_nonce(self):
        capture = self.start()
        self.audio(capture)
        self.request('pause', id=capture['id'])
        self.idle()
        model = self.bridge.meetings.model
        self.assertTrue(self.bridge.meetings.reserved())
        offer = self.request('prepare_resume', id=model.ident, revision=model.revision)['offer']
        resumed = self.request('resume', id=model.ident, offer_id=offer['id'], confirmed=True)
        self.assertTrue(resumed['ok'])
        self.assertNotEqual(resumed['capture']['nonce'], capture['nonce'])
        before = model.received_samples
        self.audio(capture)
        self.assertEqual(model.received_samples, before)
        self.audio(resumed['capture'])
        self.assertEqual(model.received_samples, before + 1600)

    def test_route_loss_or_home_pauses_and_reconnect_never_resumes_meeting(self):
        capture = self.start()
        self.audio(capture)
        self.bridge.cancel('transport_switch')
        model = self.bridge.meetings.model
        self.assertEqual((model.state, model.reason), ('paused', 'route_lost'))
        self.bridge.session.bound_route_token = 'd' * 32
        self.bridge.session.connection.route_token = 'd' * 32
        self.peer()
        before = model.snapshot()
        self.audio(capture, token='c' * 32)
        self.audio(capture)
        self.assertEqual(model.snapshot(), before)
        self.assertEqual(self.request('status')['state']['state'], 'paused')
        self.assertEqual(self.clients, [])
        self.bridge.stop()
        self.assertEqual(model.state, 'paused')

    def test_bad_native_version_never_advertises_meeting_or_accepts_audio(self):
        for version in (True, 2, '1', None):
            self.peer(version)
            self.assertFalse(self.bridge.meetings.available())
            hello = next(v for k, v in reversed(self.bridge.events) if k == 'hello')
            self.assertFalse(hello['supports_meetings'])
        self.assertIsNone(self.bridge.meetings.model)

    def test_revision_bound_pages_edits_delete_and_completed_result_survives_cancel(self):
        capture = self.start()
        self.audio(capture)
        self.request('end', id=capture['id'])
        self.idle()
        model = self.bridge.meetings.model
        page = self.request('index', id=model.ident, revision=model.revision, offset=0)['page']
        row_id = page['rows'][0]['id']
        self.assertTrue(
            self.request(
                'edit',
                id=model.ident,
                revision=model.revision,
                row_id=row_id,
                text='人工校对',
                confirmed=True,
            )['ok']
        )
        text = self.request(
            'text', id=model.ident, revision=model.revision, row_id=row_id, offset=0, original=False
        )['page']['text']
        self.assertEqual(text, '人工校对')
        before = model.snapshot()
        self.bridge.cancel('button')
        self.assertEqual(model.snapshot(), before)
        self.assertTrue(
            self.request('delete', id=model.ident, revision=model.revision, confirmed=True)['ok']
        )
        self.assertEqual(model.state, 'deleted')
        self.assertIsNone(self.bridge.meetings.worker.current)

    def test_recording_failure_update_is_real_and_not_a_new_capture_command(self):
        capture = self.start()
        self.audio(capture, nonce='0' * 32)
        self.assertEqual(self.bridge.meetings.model.received_samples, 0)
        self.audio(capture, sequence=2, frame=0)
        self.assertEqual(self.bridge.meetings.model.reason, 'protocol_error')
        self.bridge.meetings.changed.set()
        self.bridge.meetings.tick()
        event, update = self.bridge.events[-1]
        self.assertEqual(event, 'meeting_update')
        self.assertEqual(update['state']['state'], 'paused')
        self.assertNotIn('capture', update)

    def test_reservation_cannot_race_the_in_lock_vision_admission(self):
        b = self.bridge
        calls = []

        def calibrating():
            calls.append(True)
            return len(calls) > 1

        b.visual.hooks = replace(b.visual.hooks, calibrating=calibrating)
        generation = b.generation
        b.visual.begin_visual('test', 'button', 'a' * 32)
        self.assertEqual(b.generation, generation)
        self.assertTrue(b.response.queue.empty())

    def test_meeting_frame_roundtrip_and_bounds_include_dedicated_nonce(self):
        for size in (2, 320, 3200):
            packet = Packet(MEETING, 3, 4, 5, 6, bytes.fromhex('a' * 32) + b'x' * size)
            bound = packet.bind_route('c' * 32)
            self.assertEqual(receive(Fragmented(bound.encode())).unbind_route('c' * 32), packet)
        for data in (b'', bytes(16), bytes(17), bytes(3218)):
            with self.assertRaises(ValueError):
                Packet(MEETING, data=data).encode()

    def test_closing_a_pending_start_aborts_only_that_capture_and_not_a_later_resume(self):
        capture = self.start()
        old_request = self.bridge.meetings.capture_request
        self.assertTrue(self.request('abort_start', start_request_id=old_request)['ok'])
        model = self.bridge.meetings.model
        self.assertEqual(model.state, 'paused')
        offer = self.request('prepare_resume', id=model.ident, revision=model.revision)['offer']
        new = self.request('resume', id=model.ident, offer_id=offer['id'], confirmed=True)
        self.assertTrue(new['ok'])
        self.assertFalse(self.request('abort_start', start_request_id=old_request)['ok'])
        self.assertEqual(model.state, 'capturing')
        self.audio(capture)
        self.assertEqual(model.received_samples, 0)
        self.audio(new['capture'])
        self.assertEqual(model.received_samples, 1600)

    def test_capture_failure_requires_current_nonce_and_rejects_unimplemented_raw_retention(self):
        offer = self.request('prepare')['offer']
        self.assertFalse(
            self.request('start', offer_id=offer['id'], confirmed=True, save_audio=True)['ok']
        )
        self.assertIsNone(self.bridge.meetings.model)
        capture = self.start()
        self.assertFalse(
            self.request(
                'capture_failed', id=capture['id'], nonce='0' * 32, reason='capture_failed'
            )['ok']
        )
        self.assertEqual(self.bridge.meetings.model.state, 'capturing')
        self.assertTrue(
            self.request(
                'capture_failed',
                id=capture['id'],
                nonce=capture['nonce'],
                reason='credential_or_permission',
            )['ok']
        )
        self.assertEqual(
            (self.bridge.meetings.model.state, self.bridge.meetings.model.reason),
            ('paused', 'credential_or_permission'),
        )

    def test_new_meeting_claim_fences_cleanup_from_an_old_idle_cancellation(self):
        b = self.bridge
        entered, release = threading.Event(), threading.Event()
        failures = []

        def notify():
            entered.set()
            if not release.wait(2):
                raise AssertionError('Old cancellation was not released')

        def cancel():
            try:
                b.cancel('button')
            except Exception as error:
                failures.append(type(error).__name__)

        with patch.object(b.playback, 'notify', notify):
            thread = threading.Thread(target=cancel, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                old_generation = b.generation
                capture = self.start()
                self.assertGreater(capture['generation'], old_generation)
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(b.meetings.model.state, 'capturing')
        self.audio(capture)
        self.assertEqual(b.meetings.model.received_samples, 1600)

    def test_wrong_old_offer_or_completed_abort_does_not_revoke_a_fresh_confirmation(self):
        old = self.request('prepare')['offer']['id']
        fresh = self.request('prepare')['offer']['id']
        self.assertFalse(
            self.request('start', offer_id=old, confirmed=True, save_audio=False)['ok']
        )
        capture = self.request('start', offer_id=fresh, confirmed=True, save_audio=False)
        self.assertTrue(capture['ok'])
        self.assertEqual(capture['generation'], capture['capture']['generation'])
        prior_start = self.bridge.meetings.capture_request
        model = self.bridge.meetings.model
        self.request('pause', id=model.ident)
        offer = self.request('prepare_resume', id=model.ident, revision=model.revision)['offer']
        self.assertTrue(self.request('abort_start', start_request_id=prior_start)['ok'])
        self.assertTrue(
            self.request('resume', id=model.ident, offer_id=offer['id'], confirmed=True)['ok']
        )


if __name__ == '__main__':
    unittest.main()

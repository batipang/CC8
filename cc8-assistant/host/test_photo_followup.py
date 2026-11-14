"""Bounded opt-in photo ownership, real timer cleanup and cancellation interleavings."""

import threading
import time
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np

from photo_followup import PhotoFollowup, spoken_photo_question
from vision import HD_IMAGE, LEGACY_IMAGE
from reply_types import PhotoReply
import test_vision as fixtures


class Timer:
    def __init__(self, delay, callback, args):
        self.delay, self.callback, self.args = delay, callback, args
        self.started = self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire_late(self):
        self.callback(*self.args)


class PhotoFollowupTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.timers = []
        self.notices = []

        def timer(delay, callback, args):
            t = Timer(delay, callback, args)
            self.timers.append(t)
            return t

        self.cache = PhotoFollowup(
            lambda ident, generation: self.notices.append((ident, generation)),
            clock=lambda: self.now,
            timer_factory=timer,
        )
        self.addCleanup(self.cache.clear)

    def arm(self, generation=1):
        return self.cache.arm(generation, fixtures.jpeg(1280, 960), HD_IMAGE, consent=True)

    def test_metadata_never_contains_pixels_and_invalid_owner_cannot_consume(self):
        state = self.arm()
        self.assertEqual(set(state), {'photo_id', 'generation', 'width', 'height', 'ttl_ms'})
        self.assertEqual((state['width'], state['height'], state['ttl_ms']), (1280, 960, 120000))
        for ident, generation in [
            (state['photo_id'], 2),
            (state['photo_id'], True),
            ('f' * 32, 1),
            ('bad', 1),
            (None, 1),
        ]:
            self.assertIsNone(self.cache.consume(ident, generation))
            self.assertEqual(self.cache.metadata(1), state)
        self.assertTrue(self.timers[0].started and self.timers[0].daemon)

    def test_atomic_one_use_and_explicit_successor_do_not_extend_original_expiry(self):
        state = self.arm()
        self.now = 130
        photo = self.cache.consume(state['photo_id'], 1)
        self.assertEqual(photo.image, fixtures.jpeg(1280, 960))
        self.assertNotIn('image=', repr(photo))
        self.assertIsNone(self.cache.consume(state['photo_id'], 1))
        self.assertIsNone(self.cache.metadata(1))
        next_state = self.cache.arm(
            2, photo.image, photo.policy, consent=True, expires=photo.expires
        )
        self.assertNotEqual(next_state['photo_id'], state['photo_id'])
        self.assertEqual(next_state['ttl_ms'], 90000)
        self.assertTrue(self.timers[0].cancelled)
        self.assertEqual(self.timers[1].delay, 90)

    def test_late_cancelled_timer_cannot_remove_new_photo(self):
        first = self.arm()
        self.now = 150
        fresh = self.arm(2)
        self.now = 220
        self.timers[0].fire_late()
        self.assertEqual(self.notices, [])
        self.assertEqual(self.cache.metadata(2)['photo_id'], fresh['photo_id'])
        self.assertEqual(self.cache.metadata(2)['ttl_ms'], 50000)
        self.assertIsNone(self.cache.consume(first['photo_id'], 1))
        # At its own deadline the new picture is inaccessible even if that
        # timer has not received a scheduling slot yet.
        self.now = 270
        self.assertIsNone(self.cache.metadata(2))
        self.assertIsNone(self.cache.consume(fresh['photo_id'], 2))

    def test_expiry_releases_pixels_before_content_free_notification(self):
        state = self.arm()
        self.now = 220
        self.timers[0].fire_late()
        self.assertEqual(self.notices, [(state['photo_id'], 1)])
        self.assertIsNone(self.cache._photo)
        self.timers[0].fire_late()
        self.assertEqual(len(self.notices), 1)

    def test_invalid_replacement_keeps_current_owner(self):
        state = self.arm()
        for generation in (True, -1, 2**31, '1'):
            with self.assertRaises(ValueError):
                self.arm(generation)
        for expires in (True, 100, 221, float('inf'), float('nan'), '220'):
            with self.assertRaises(ValueError):
                self.cache.arm(2, fixtures.jpeg(), LEGACY_IMAGE, consent=True, expires=expires)
        with self.assertRaises(ValueError):
            self.cache.arm(2, fixtures.jpeg(), None, consent=True)
        self.assertEqual(self.cache.metadata(1), state)
        self.assertFalse(self.timers[0].cancelled)

    def test_opt_in_must_be_a_real_boolean_and_never_replaces_another_owner(self):
        state = self.arm()
        for consent in (False, None, 1, 'yes'):
            with self.assertRaises(ValueError):
                self.cache.arm(2, fixtures.jpeg(), LEGACY_IMAGE, consent=consent)
        self.assertEqual(self.cache.metadata(1), state)
        self.assertFalse(self.timers[0].cancelled)

    def test_real_timer_releases_without_any_read_or_new_request(self):
        expired = threading.Event()
        cache = PhotoFollowup(lambda ident, generation: expired.set())
        self.addCleanup(cache.clear)
        cache.arm(1, fixtures.jpeg(), LEGACY_IMAGE, consent=True, expires=time.monotonic() + 0.05)
        self.assertTrue(expired.wait(1))
        self.assertIsNone(cache._photo)

    def test_timer_start_failure_fails_closed_without_leaving_an_unbounded_photo(self):
        state = self.arm()
        with patch.object(Timer, 'start', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                self.arm(2)
        self.assertIsNone(self.cache._photo)
        self.assertIsNone(self.cache.consume(state['photo_id'], 1))
        self.assertTrue(all(t.cancelled for t in self.timers))

    def test_two_concurrent_consumers_cannot_both_take_the_same_photo(self):
        state = self.arm()
        barrier = threading.Barrier(3)
        results = []

        def consume():
            barrier.wait()
            results.append(self.cache.consume(state['photo_id'], 1))

        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(r is not None for r in results), 1)


class PhotoControllerTests(unittest.TestCase):
    def bridge(self, capability=True):
        bridge = fixtures.BridgeVisionTests().bridge()
        bridge.visual.followup = PhotoFollowup(bridge.visual.photo_expired, timer_factory=Timer)

        def cleanup():
            with bridge.guard:
                bridge.visual.invalidate()

        self.addCleanup(cleanup)
        bridge.visual.update_peer({'vision_followup_version': 1} if capability else {})
        bridge.received_photos = []
        bridge.answer = lambda text, generation, image=None: bridge.received_photos.append(
            (text, image)
        )
        return bridge

    def capture(self, bridge, consent=True):
        bridge.on_event({'event': 'vision_request', 'nonce': 'a' * 32})
        event = bridge.event

        def capture(name, **fields):
            event(name, **fields)
            if name == 'camera_capture':
                rows = list(fixtures.events(bridge.visual.inbox.pending))
                rows[0]['retain_for_followup'] = consent
                for row in rows:
                    bridge.on_event(row)

        bridge.event = capture
        bridge.answer_visual(bridge.job)
        bridge.event = event
        return bridge.visual.followup.metadata(bridge.session.generation)

    def request(self, bridge, state, text='这张照片上写了什么？'):
        bridge.on_event(
            dict(
                event='photo_followup_request',
                nonce='b' * 32,
                photo_id=state['photo_id'],
                text=text,
            )
        )

    def test_default_remains_ram_release_without_a_reusable_photo(self):
        bridge = self.bridge()
        self.assertIsNone(self.capture(bridge, consent=False))
        self.assertEqual(len(bridge.received_photos), 1)
        self.assertFalse(bridge.session.active)
        self.assertFalse(any(name == 'photo_followup_ready' for name, _ in bridge.events))

    def test_explicit_followup_uses_exact_pixels_once_without_reopening_camera(self):
        bridge = self.bridge()
        state = self.capture(bridge)
        expiry = bridge.visual.followup._photo.expires
        self.assertIsNotNone(state)
        captures = sum(name == 'camera_capture' for name, _ in bridge.events)
        self.request(bridge, state)
        job = bridge.job
        self.assertIsInstance(job, PhotoReply)
        self.assertEqual(job.image, fixtures.jpeg())
        self.request(bridge, state)  # Duplicate cannot replace the admitted job.
        self.assertIs(bridge.job, job)
        bridge.answer_visual(job)
        self.assertEqual(bridge.received_photos[-1], ('这张照片上写了什么？', fixtures.jpeg()))
        self.assertIsNone(job.image)
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), captures)
        current = bridge.visual.followup.metadata(bridge.session.generation)
        self.assertNotEqual(current['photo_id'], state['photo_id'])
        self.assertEqual(bridge.visual.followup._photo.expires, expiry)
        self.assertFalse(bridge.session.active)

    def test_cancelled_queued_question_loses_pixels_before_worker_dispatch(self):
        bridge = self.bridge()
        state = self.capture(bridge)
        self.request(bridge, state)
        job = bridge.job
        bridge.cancel('button')
        self.assertIsNone(job.image)
        bridge.respond_job(job)
        self.assertEqual(len(bridge.received_photos), 1)
        self.assertIsNone(bridge.visual.followup._photo)

    def test_invalid_question_cannot_consume_photo_or_cancel_current_owner(self):
        bridge = self.bridge()
        state = self.capture(bridge)
        generation = bridge.session.generation
        for text in ('', 'x' * 1001, '\ud800', True):
            with self.assertRaises(ValueError):
                self.request(bridge, state, text)
            self.assertEqual(bridge.session.generation, generation)
            self.assertEqual(
                bridge.visual.followup.metadata(generation)['photo_id'], state['photo_id']
            )

    def test_unnegotiated_retention_is_rejected_without_model_or_camera_retry(self):
        bridge = self.bridge(capability=False)
        self.assertIsNone(self.capture(bridge))
        self.assertEqual(bridge.received_photos, [])
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)
        self.assertIsNone(bridge.visual.followup._photo)

    def test_cancellation_during_answer_cannot_revive_a_retained_image(self):
        bridge = self.bridge()
        bridge.answer = lambda *args, **kwargs: bridge.cancel('button')
        self.assertIsNone(self.capture(bridge))
        self.assertIsNone(bridge.visual.followup._photo)
        self.assertFalse(any(name == 'photo_followup_ready' for name, _ in bridge.events))

    def test_expired_queued_question_never_falls_back_to_a_new_photo(self):
        bridge = self.bridge()
        state = self.capture(bridge)
        self.request(bridge, state)
        job = bridge.job
        job.expires = 0
        bridge.answer_visual(job)
        self.assertIsNone(job.image)
        self.assertEqual(len(bridge.received_photos), 1)

    def test_queued_photo_is_released_at_deadline_even_without_a_worker_slot(self):
        bridge = self.bridge()
        state = self.capture(bridge)
        bridge.visual.followup._photo = replace(
            bridge.visual.followup._photo, expires=time.monotonic() + 0.05
        )
        self.request(bridge, state)
        deadline = time.monotonic() + 1
        while bridge.job.image is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNone(bridge.job.image)
        self.assertEqual(len(bridge.received_photos), 1)
        bridge.answer_visual(bridge.job)
        self.assertEqual(len(bridge.received_photos), 1)

    def test_retired_queue_timer_cannot_erase_a_new_question(self):
        bridge = self.bridge()
        timers = []

        def timer(*args, **kwargs):
            created = Timer(*args, **kwargs)
            timers.append(created)
            return created

        with patch('vision_controller.threading.Timer', side_effect=timer):
            self.request(bridge, self.capture(bridge))
            previous = bridge.job
            bridge.cancel('button')
            self.assertIsNone(previous.image)
            self.assertTrue(timers[0].cancelled)
            self.request(bridge, self.capture(bridge))
            current = bridge.job
            timers[0].fire_late()
            self.assertEqual(current.image, fixtures.jpeg())
            self.assertIs(bridge.visual.followup_job, current)

    def test_queue_timer_start_failure_releases_pixels_and_does_not_retry(self):
        bridge = self.bridge()
        state = self.capture(bridge)
        broken = Timer(0, lambda: None, ())
        with (
            patch('vision_controller.threading.Timer', return_value=broken),
            patch.object(broken, 'start', side_effect=RuntimeError('timer unavailable')),
        ):
            with self.assertRaises(RuntimeError):
                self.request(bridge, state)
        self.assertTrue(broken.cancelled)
        self.assertIsNone(bridge.visual.followup_job)
        self.assertIsNone(bridge.visual.followup_job_timer)
        self.assertEqual(len(bridge.received_photos), 1)
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)

    def test_explicit_forget_removes_only_this_photo_without_starting_or_cancelling_a_task(self):
        bridge = self.bridge()
        state = self.capture(bridge)
        generation = bridge.session.generation
        bridge.on_event(dict(event='photo_followup_forget', photo_id=state['photo_id']))
        self.assertIsNone(bridge.visual.followup._photo)
        self.assertEqual(bridge.session.generation, generation)
        self.assertEqual(len(bridge.received_photos), 1)
        self.assertFalse(bridge.session.active)
        self.assertEqual(bridge.events[-1][0], 'photo_followup_expired')
        self.assertEqual(bridge.events[-1][1]['photo_id'], state['photo_id'])

    def test_stale_forget_cannot_erase_a_newer_photograph(self):
        bridge = self.bridge()
        old = self.capture(bridge)
        fresh = self.capture(bridge)
        generation = bridge.session.generation
        bridge.on_event(dict(event='photo_followup_forget', photo_id=old['photo_id']))
        self.assertEqual(bridge.visual.followup.metadata(generation)['photo_id'], fresh['photo_id'])
        self.assertEqual(bridge.events[-1][0], 'photo_followup_forget_rejected')
        self.assertEqual(bridge.session.generation, generation)

    def test_bad_forget_is_rejected_before_modifying_any_owner(self):
        bridge = self.bridge()
        state = self.capture(bridge)
        generation = bridge.session.generation
        for ident in (None, True, '', 'wrong'):
            with self.assertRaises(ValueError):
                bridge.on_event(dict(event='photo_followup_forget', photo_id=ident))
            self.assertEqual(
                bridge.visual.followup.metadata(generation)['photo_id'], state['photo_id']
            )
            self.assertEqual(bridge.session.generation, generation)

    def test_optional_retention_failure_preserves_completed_image_analysis(self):
        bridge = self.bridge()
        with patch.object(Timer, 'start', side_effect=RuntimeError('unavailable')):
            self.assertIsNone(self.capture(bridge))
        self.assertEqual(len(bridge.received_photos), 1)
        self.assertEqual(bridge.metrics['vision_last_result'], 'completed')
        self.assertEqual(bridge.metrics['photo_followup_retention_failed'], 1)
        self.assertIsNone(bridge.visual.followup._photo)

    def test_uncertain_queue_admission_releases_pixels_without_model_retry(self):
        bridge = self.bridge()
        state = self.capture(bridge)

        def uncertain(job):
            bridge.job = job
            raise RuntimeError('queue failed after accepting')

        bridge.enqueue_response = uncertain
        with self.assertRaisesRegex(RuntimeError, 'queue failed'):
            self.request(bridge, state)
        self.assertIsNone(bridge.job.image)
        self.assertIsNone(bridge.visual.followup_job)
        self.assertIsNone(bridge.visual.followup._photo)
        bridge.respond_job(bridge.job)
        self.assertEqual(len(bridge.received_photos), 1)

    def test_explicit_voice_followup_keeps_capture_owner_and_original_expiry_without_camera(self):
        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 9
        original = self.capture(bridge)
        expiry = bridge.visual.followup._photo.expires
        generation = bridge.cancel('voice')
        transferred = bridge.visual.followup.metadata(generation)
        self.assertNotEqual(original['photo_id'], transferred['photo_id'])
        self.assertEqual(bridge.visual.followup._photo.expires, expiry)
        self.assertTrue(
            bridge.recognition.hooks.photo_voice(
                '大黄蜂，追问这张照片，上面写了什么？', generation, False
            )
        )
        self.assertIsInstance(bridge.job, PhotoReply)
        self.assertEqual(bridge.job.origin, 'followup_voice')
        bridge.answer_visual(bridge.job)
        self.assertEqual(bridge.received_photos[-1], ('上面写了什么？', fixtures.jpeg()))
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)
        self.assertEqual(bridge.visual.followup._photo.expires, expiry)

    def test_gpt_live_followup_uses_the_existing_photo_without_opening_camera(self):
        from gpt_live_trial import LiveTrial

        bridge = self.bridge()
        bridge.session.active = True
        bridge.session.epoch = 9
        self.capture(bridge)
        expiry = bridge.visual.followup._photo.expires
        owner = SimpleNamespace(stop=threading.Event())
        trial = bridge.live = LiveTrial(bridge, 'fixture-only', 'marin')
        trial.session = owner
        trial.near_at = time.monotonic()
        with patch.object(trial, 'eligible', return_value=True):
            trial.scope = bridge.session.capture_owner(), bridge.generation
            self.assertTrue(trial.delegate(owner, 'photo', '大黄蜂，追问这张照片，这是什么？'))
        self.assertTrue(owner.stop.is_set())
        self.assertIsInstance(bridge.job, PhotoReply)
        self.assertEqual(bridge.job.origin, 'followup_voice')
        bridge.answer_visual(bridge.job)
        self.assertEqual(bridge.received_photos[-1], ('这是什么？', fixtures.jpeg()))
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)
        self.assertEqual(bridge.visual.followup._photo.expires, expiry)

    def test_ordinary_voice_and_stop_drop_photo_before_normal_dispatch(self):
        for text, command in [('查一下天气', False), ('停一下', True)]:
            bridge = self.bridge()
            bridge.session.active = True
            self.capture(bridge)
            generation = bridge.cancel('voice')
            self.assertIsNotNone(bridge.visual.followup.metadata(generation))
            self.assertFalse(bridge.recognition.hooks.photo_voice(text, generation, command))
            self.assertIsNone(bridge.visual.followup._photo)
            self.assertEqual(len(bridge.received_photos), 1)

    def test_changed_microphone_owner_cannot_inherit_a_photo(self):
        bridge = self.bridge()
        bridge.session.active = True
        self.capture(bridge)
        bridge.session.epoch += 1
        generation = bridge.cancel('voice')
        self.assertIsNone(bridge.visual.followup._photo)
        self.assertTrue(
            bridge.recognition.hooks.photo_voice('追问这张照片，是什么？', generation, False)
        )
        self.assertEqual(len(bridge.received_photos), 1)
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)

    def test_stale_voice_callback_cannot_erase_a_newer_picture(self):
        bridge = self.bridge()
        bridge.session.active = True
        self.capture(bridge)
        old_generation = bridge.cancel('voice')
        fresh = self.capture(bridge)
        self.assertTrue(bridge.recognition.hooks.photo_voice('普通旧问题', old_generation, False))
        self.assertEqual(
            bridge.visual.followup.metadata(bridge.session.generation)['photo_id'],
            fresh['photo_id'],
        )

    def test_photo_reference_without_question_is_explicit_and_does_not_retake(self):
        bridge = self.bridge()
        bridge.session.active = True
        self.capture(bridge)
        generation = bridge.cancel('voice')
        self.assertTrue(bridge.recognition.hooks.photo_voice('追问这张照片', generation, False))
        self.assertIsNotNone(bridge.visual.followup.metadata(generation))
        self.assertEqual(len(bridge.received_photos), 1)
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)

    def test_spoken_photo_command_requires_explicit_unquoted_present_reference(self):
        for text in ('追问这张照片，文字是什么意思？', 'Bumblebee, 追问这张照片：文字是什么意思？'):
            self.assertEqual(spoken_photo_question(text), '文字是什么意思？')
        for text in (
            '不要追问这张照片',
            '“追问这张照片”是什么意思',
            '查一下照片教程',
            '这个多少钱？',
        ):
            self.assertIsNone(spoken_photo_question(text))
        self.assertEqual(spoken_photo_question('追问这张照片'), '')

    def recognize_photo(
        self,
        *,
        risky=False,
        credible=30,
        echo=False,
        wake=True,
        tool=False,
        text='追问这张照片，上面写了什么？',
    ):
        from test_weak_near import captured

        bridge = self.bridge()
        bridge.session.active = bridge.session.awake = True
        bridge.session.wake = False
        original = self.capture(bridge)
        expires = bridge.visual.followup._photo.expires
        generation = bridge.cancel('voice')
        calls = []
        bridge.speech = SimpleNamespace(recognize=lambda pcm: calls.append('asr') or text)
        bridge.recognition.hooks = replace(
            bridge.recognition.hooks,
            wake_allowed=lambda: wake,
            tool_sample_rejection=lambda *args: 'not_open' if tool else None,
        )
        key = bridge.diagnostics.begin('voice')
        bridge.diagnostics.capture(key, captured(credible=credible, risky=risky))
        bridge.recognition.enqueue(
            (
                generation,
                np.zeros(16000),
                risky,
                [text] if echo else [],
                dict(echo_risk=risky, captured_at=time.monotonic()),
                None,
                key,
            )
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            row = next(r for r in bridge.diagnostics.report()['requests'] if r['request_id'] == key)
            if 'gate' in row and not bridge.recognition.busy:
                return bridge, row, original, expires, calls
            time.sleep(0.005)
        self.fail('Photo recognition did not finish')

    def test_real_recognition_routes_explicit_photo_after_all_admission_gates(self):
        for risky in (False, True):
            with self.subTest(playback_risk=risky):
                bridge, row, _, expires, calls = self.recognize_photo(risky=risky)
                self.assertEqual(row['gate'], 'accepted')
                self.assertEqual(calls, ['asr'])
                self.assertIsInstance(bridge.job, PhotoReply)
                self.assertEqual(bridge.job.text, '上面写了什么？')
                self.assertEqual(bridge.job.expires, expires)
                bridge.answer_visual(bridge.job)
                self.assertEqual(bridge.received_photos[-1], ('上面写了什么？', fixtures.jpeg()))
                self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)

    def test_real_recognition_routes_new_ocr_capture_and_rejects_closed_wake(self):
        from vision import OCR_PROMPT

        text = '读一下这张纸上的字'
        bridge, row, _, _, calls = self.recognize_photo(text=text)
        self.assertEqual(row['gate'], 'accepted')
        self.assertEqual(calls, ['asr'])
        self.assertEqual(bridge.job.origin, 'voice')
        self.assertEqual(bridge.job.text, OCR_PROMPT)
        original_event = bridge.event

        def send_current_frame(name, **fields):
            original_event(name, **fields)
            if name == 'camera_capture':
                for event in fixtures.events(bridge.visual.inbox.pending):
                    bridge.on_event(event)

        with patch.object(bridge, 'event', send_current_frame):
            bridge.answer_visual(bridge.job)
        self.assertEqual(bridge.received_photos[-1], (OCR_PROMPT, fixtures.jpeg()))
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 2)

        bridge, row, _, _, calls = self.recognize_photo(text=text, wake=False)
        self.assertEqual(row['gate'], 'wake_closed')
        self.assertEqual(calls, [])
        self.assertEqual(sum(name == 'camera_capture' for name, _ in bridge.events), 1)

    def test_rejected_real_recognition_cannot_consume_photo_or_start_followup(self):
        for options, gate, attempts in (
            (dict(risky=True, echo=True), 'echo_text', 1),
            (dict(risky=True, credible=2), 'weak_near', 1),
            (dict(wake=False), 'wake_closed', 0),
            (dict(tool=True), 'tool_sample_guard', 1),
        ):
            with self.subTest(gate=gate):
                bridge, row, _, expires, calls = self.recognize_photo(**options)
                self.assertEqual(row['gate'], gate)
                self.assertEqual(len(calls), attempts)
                self.assertNotIsInstance(bridge.job, PhotoReply)
                self.assertEqual(bridge.visual.followup._photo.expires, expires)
                self.assertEqual(len(bridge.received_photos), 1)
                self.assertFalse(
                    any(
                        name == 'vision_begin' and fields.get('origin') == 'followup_voice'
                        for name, fields in bridge.events
                    )
                )


if __name__ == '__main__':
    unittest.main()

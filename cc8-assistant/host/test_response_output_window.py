"""Fixed-clock response observation through the real playback publication path."""

import json
import threading
import unittest
from unittest.mock import patch

import numpy as np

from bridge import Bridge
from protocol import EVENT, receive
from test_playback_evidence import feed, public_signal, scene
from test_runtime import Fragmented


class Clock:
    def __init__(self):
        self.now = 0.0
        self.timers = []

    def timer(self, seconds, callback):
        clock = self

        class Timer:
            def __init__(self):
                self.cancelled = False
                self.deadline = clock.now + seconds

            def start(self):
                clock.timers.append(self)

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.cancelled = True
                callback()

        return Timer()

    def advance(self, seconds):
        self.now += seconds
        for timer in tuple(self.timers):
            if not timer.cancelled and self.now >= timer.deadline:
                timer.fire()


class ResponseOutputWindowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(patch('playback_evidence.threading.Timer', self.clock.timer))
        self.enterContext(patch('capture_pipeline.time.monotonic', lambda: self.clock.now))
        self.enterContext(patch('playback_controller.time.sleep', self.clock.advance))
        self.enterContext(patch('test_playback_evidence.START', 30_000_000_000))
        self.bridge = Bridge(None, None)
        self.bridge.session.active = True
        self.bridge.session.epoch = 7
        self.bridge.session.generation = 101
        self.bridge.save_status = lambda: None
        self.capture = self.bridge.capture
        self.at_begin = []
        owner = self

        class Device:
            def sendall(self, data):
                packet = receive(Fragmented(data))
                if packet.kind != EVENT:
                    return
                event = json.loads(packet.data)
                if event['event'] == 'play_begin':
                    owner.at_begin.append(owner.capture.evidence.current)
                if event['event'] == 'play_end':
                    owner.bridge.on_event({'event': 'play_done', 'generation': packet.generation})

        self.bridge.session.connection = Device()
        self.addCleanup(self.capture.clear_response_probe)

    def bind(self, *, elapsed_ms=600):
        token = self.capture.arm_response_probe(1)
        self.capture.bind_response_probe(
            '大黄蜂，一加一等于几？', 101, 30_000_000_000 - elapsed_ms * 1_000_000
        )
        return token

    def play(self):
        return self.bridge.playback.play_stream(
            iter([(public_signal() * 32768).astype('<i2')]), '二。', 101
        )

    def test_model_wait_retains_no_pcm_and_starts_budget_at_output_without_clock_capability(self):
        token = self.bind(elapsed_ms=19600)
        self.capture.evidence.microphone(7, 100, np.ones(320))
        self.assertIsNone(self.capture.evidence.current)
        self.assertFalse(self.capture.evidence.mic.size)
        self.assertEqual(self.clock.timers, [])
        self.clock.advance(19.5)
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'observing'})
        self.assertFalse(self.bridge.playback.device_can_report_clock)
        self.assertTrue(self.play())
        self.assertIsNotNone(self.at_begin[0])
        self.assertEqual(len(self.clock.timers), 1)
        self.assertEqual(self.clock.timers[0].deadline, 39.5)
        reference = public_signal()
        feed(self.capture.evidence, reference, scene(reference))
        receipt = self.capture.read_response_probe(token)
        self.assertEqual(receipt['status'], 'observed')
        self.assertAlmostEqual(receipt['last_voiced_to_confirmed_output_block_ms'], 19720, delta=1)
        self.assertFalse(self.capture.evidence.mic.size)

    def test_more_than_twenty_seconds_of_model_wait_does_not_expire_before_output(self):
        token = self.bind(elapsed_ms=25600)
        self.clock.advance(25)
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'observing'})
        self.assertIsNone(self.capture.evidence.current)
        self.assertTrue(self.play())
        reference = public_signal()
        feed(self.capture.evidence, reference, scene(reference))
        # The existing 20s end-to-end interval bound is independent of probe lifetime.
        self.assertEqual(
            self.capture.read_response_probe(token),
            {'status': 'unconfirmed', 'generation': 101, 'reason': 'interval_out_of_bounds'},
        )

    def test_unbound_arm_survives_admission_cancel_but_bound_owner_cannot_restart(self):
        token = self.capture.arm_response_probe(1)
        generation = self.bridge.cancel('voice_admission')
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'waiting'})
        self.capture.bind_response_probe('一加一等于几？', generation, 29_400_000_000)
        self.assertIsNone(self.capture.evidence.current)
        self.bridge.cancel('new_request')
        self.assertEqual(
            self.capture.read_response_probe(token),
            {'status': 'unconfirmed', 'generation': generation, 'reason': 'cancelled'},
        )
        self.assertFalse(self.play())
        self.assertEqual(self.clock.timers, [])

    def test_pending_absolute_deadline_never_arms_after_expiry(self):
        token = self.bind()
        self.clock.advance(120)
        self.assertTrue(self.play())
        self.assertEqual(self.at_begin, [None])
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'unavailable'})
        self.assertEqual(self.clock.timers, [])

    def test_output_budget_is_clipped_by_absolute_deadline_and_not_reset_by_next_clause(self):
        token = self.bind()
        self.clock.advance(115)
        self.assertTrue(self.play())
        first = self.at_begin[0]
        self.assertIsNotNone(first)
        self.assertTrue(self.play())
        self.assertIs(self.at_begin[1], first)
        self.assertEqual(len(self.clock.timers), 1)
        self.assertEqual(self.clock.timers[0].deadline, 120)
        self.clock.advance(5)
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'unavailable'})
        self.assertTrue(first.done.is_set())
        self.assertIsNone(self.capture.evidence.current)

    def test_pending_reset_does_not_publish_for_successor_owner(self):
        token = self.bind()
        self.capture.reset_session()
        new_token = self.capture.arm_response_probe(1)
        self.assertTrue(self.play())
        self.assertEqual(self.at_begin, [None])
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'unavailable'})
        self.assertEqual(self.capture.read_response_probe(new_token), {'status': 'waiting'})
        self.assertEqual(self.clock.timers, [])

    def test_stale_generation_epoch_and_inactive_capture_cannot_start_observation(self):
        for state in ('generation', 'epoch', 'inactive'):
            with self.subTest(state=state):
                self.capture.clear_response_probe()
                self.bridge.session.active = True
                self.bridge.session.epoch = 7
                self.bridge.session.generation = 101
                token = self.bind()
                if state == 'generation':
                    self.bridge.session.generation = 102
                elif state == 'epoch':
                    self.bridge.session.epoch = 8
                else:
                    self.bridge.session.active = False
                self.capture.begin_response_output(101)
                self.assertIsNone(self.capture.evidence.current)
                self.assertEqual(self.clock.timers, [])
                if state == 'epoch':
                    self.assertEqual(
                        self.capture.read_response_probe(token), {'status': 'unavailable'}
                    )

    def test_timer_start_failure_does_not_fail_speech_or_retain_pcm(self):
        token = self.bind()

        def unavailable(seconds, callback):
            timer = self.clock.timer(seconds, callback)
            timer.start = lambda: (_ for _ in ()).throw(RuntimeError('unavailable'))
            return timer

        with patch('playback_evidence.threading.Timer', unavailable):
            self.assertTrue(self.play())
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'probe_setup_failed'})
        self.assertEqual(self.at_begin, [None])
        self.assertIsNone(self.capture.evidence.current)
        self.assertFalse(self.capture.evidence.mic.size)

    def test_timer_construction_failure_does_not_leave_an_unowned_probe(self):
        token = self.bind()
        with patch('playback_evidence.threading.Timer', side_effect=RuntimeError('unavailable')):
            self.assertTrue(self.play())
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'probe_setup_failed'})
        self.assertIsNone(self.capture.evidence.current)
        self.assertIsNone(self.capture.evidence.timer)
        self.assertFalse(self.capture.evidence.mic.size)

    def test_reentrant_teardown_during_timer_start_keeps_new_waiting_owner(self):
        token = self.bind()
        successor = []

        def teardown(seconds, callback):
            timer = self.clock.timer(seconds, callback)

            def start():
                self.capture.clear_response_probe()
                successor.append(self.capture.arm_response_probe(1))

            timer.start = start
            return timer

        with patch('playback_evidence.threading.Timer', teardown):
            self.assertTrue(self.play())
        self.assertEqual(self.at_begin, [None])
        self.assertIsNone(self.capture.evidence.current)
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'unavailable'})
        self.assertEqual(self.capture.read_response_probe(successor[0]), {'status': 'waiting'})

    def test_concurrent_clear_waits_for_timer_publication_and_cleans_exact_probe(self):
        token = self.bind()
        entered, release, attempted, cleared = (threading.Event() for _ in range(4))
        failures = []

        def slow_timer(seconds, callback):
            timer = self.clock.timer(seconds, callback)

            def start():
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('test timer was not released')
                self.clock.timers.append(timer)

            timer.start = start
            return timer

        def start_output():
            try:
                self.capture.begin_response_output(101)
            except Exception as error:
                failures.append(type(error).__name__)

        def clear():
            attempted.set()
            self.capture.clear_response_probe()
            cleared.set()

        output = threading.Thread(target=start_output)
        cleanup = threading.Thread(target=clear)
        with patch('playback_evidence.threading.Timer', slow_timer):
            output.start()
            try:
                self.assertTrue(entered.wait(1))
                cleanup.start()
                self.assertTrue(attempted.wait(1))
                self.assertFalse(cleared.wait(0.02))
            finally:
                release.set()
                output.join(2)
                if cleanup.ident is not None:
                    cleanup.join(2)
        self.assertFalse(output.is_alive() or cleanup.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(cleared.is_set())
        self.assertEqual(self.capture.read_response_probe(token), {'status': 'unavailable'})
        self.assertIsNone(self.capture.evidence.current)
        self.assertTrue(self.clock.timers[0].cancelled)


if __name__ == '__main__':
    unittest.main()

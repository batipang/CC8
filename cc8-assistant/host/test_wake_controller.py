"""Local authorization/window boundaries and stale-frame lock ordering."""

from types import SimpleNamespace
import threading
import unittest
import numpy as np
from wake_controller import WakeController, WakeHooks, WAITING


class FakeDetector:
    def __init__(self):
        self.word = '大黄蜂'
        self.calls = 0
        self.resets = []
        self.alternate_frames = []

    def feed(self, pcm, *, alternate_pcm=None):
        self.calls += 1
        self.alternate_frames.append(alternate_pcm)
        return self.word

    def reset(self, *, clear_metrics=True):
        self.resets.append(clear_metrics)


class WakeControllerTests(unittest.TestCase):
    def setUp(self):
        self.guard = threading.RLock()
        self.state = SimpleNamespace(
            active=True, generation=100, awake=False, wake=True, busy=False
        )
        self.now = 0
        self.statuses, self.logs, self.metrics = [], [], {}
        self.touches = 0
        self.cancelled = []
        self.detector = FakeDetector()
        self.hooks = WakeHooks(
            lock=self.guard,
            active=lambda: self.state.active,
            generation=lambda: self.state.generation,
            awake=lambda: self.state.awake,
            wake_mode=lambda: self.state.wake,
            capture_current=lambda nanos: True,
            busy=lambda: self.state.busy,
            set_awake=self.set_awake,
            touch_activity=self.touch,
            cancel=self.cancel,
            status=lambda *args: self.statuses.append(args),
            metric=self.metric,
            log=lambda name, **fields: self.logs.append((name, fields)),
        )
        self.controller = WakeController(
            self.hooks, self.detector, window_seconds=15, clock=lambda: self.now
        )

    def set_awake(self, value):
        self.state.awake = value
        self.controller.awake_changed(value)

    def touch(self):
        self.touches += 1
        self.controller.touch()

    def cancel(self, reason):
        self.cancelled.append(reason)
        self.state.generation += 1
        self.controller.reset('cancel')
        return self.state.generation

    def metric(self, name, value=None, *, increment=False):
        self.metrics[name] = self.metrics.get(name, 0) + 1 if increment else value

    def feed(self, **fields):
        return self.controller.feed(np.zeros(320), self.state.generation, capture_nanos=0, **fields)

    def test_english_input_snapshot_supports_legacy_detector_and_is_independent(self):
        self.assertIsNone(self.controller.english_input_samples())
        self.detector.english_input_samples = (640, 160)
        counts = self.controller.english_input_samples()
        self.detector.english_input_samples = (800, 320)
        self.assertEqual(counts, {'primary': 640, 'alternate': 160})
        self.assertEqual(
            self.controller.english_input_samples(), {'primary': 800, 'alternate': 320}
        )

    def test_english_input_snapshot_does_not_wait_for_stalled_decoder(self):
        held = threading.Event()
        release = threading.Event()

        def hold_decoder():
            with self.controller.native_lock:
                held.set()
                release.wait(2)

        worker = threading.Thread(target=hold_decoder)
        worker.start()
        try:
            self.assertTrue(held.wait(1))
            self.detector.english_input_samples = (640, 160)
            self.assertIsNone(self.controller.english_input_samples())
            self.assertTrue(worker.is_alive())
        finally:
            release.set()
            worker.join(1)
        self.assertEqual(
            self.controller.english_input_samples(), {'primary': 640, 'alternate': 160}
        )

    def test_local_name_authorizes_once_and_continuous_standby_also_requires_wake(self):
        self.assertFalse(self.controller.allowed())
        self.assertTrue(self.feed())
        self.assertTrue(self.controller.allowed())
        self.assertEqual(self.controller.deadline, 15)
        self.assertEqual(self.touches, 1)
        self.assertFalse(self.feed())
        self.assertEqual(self.detector.calls, 1)
        self.state.wake = False
        self.set_awake(False)
        self.detector.word = 'Bumblebee'
        self.assertFalse(self.controller.allowed())
        self.assertTrue(self.feed())
        self.assertIsNone(self.controller.deadline)
        self.now = 1000
        self.controller.tick()
        self.assertTrue(self.state.awake)

    def test_window_cannot_expire_mid_work_and_expires_after_idle_without_capture_change(self):
        self.assertTrue(self.feed())
        self.now = 14
        self.controller.tick()
        self.assertTrue(self.state.awake)
        self.state.busy = True
        self.now = 30
        self.controller.tick()
        self.assertEqual(self.controller.deadline, 45)
        self.state.busy = False
        self.now = 46
        self.controller.tick()
        self.assertFalse(self.state.awake)
        self.assertTrue(self.state.active)
        self.assertEqual(self.cancelled, ['wake_timeout'])
        self.assertEqual(self.statuses[-1], ('listening', WAITING))
        self.assertIsNone(self.controller.deadline)

    def test_echo_name_is_rejected_but_new_local_name_can_wake(self):
        self.assertFalse(self.feed(echo_risk=True, render_text='我是大黄蜂，你的助手'))
        self.assertFalse(self.state.awake)
        self.assertEqual(self.touches, 0)
        self.assertEqual(self.metrics['wake_render_rejections'], 1)
        self.assertTrue(self.feed(echo_risk=True, render_text='现在是下午三点'))
        self.assertTrue(self.state.awake)

    def test_alternate_pcm_only_reaches_detector_without_playback_risk(self):
        self.detector.word = ''
        alternate = np.ones(320)
        self.assertFalse(self.feed(alternate_pcm=alternate, echo_risk=True))
        self.assertIsNone(self.detector.alternate_frames[-1])
        self.detector.word = 'Bumblebee'
        self.assertTrue(self.feed(alternate_pcm=alternate))
        self.assertIs(self.detector.alternate_frames[-1], alternate)
        self.assertEqual(self.metrics['local_wake_english_count'], 1)

    def test_missing_detector_or_inactive_session_never_falls_back_to_cloud(self):
        self.controller.detector = None
        self.assertFalse(self.feed())
        self.state.awake = True
        self.assertFalse(self.controller.allowed())
        self.state.wake = False
        self.assertTrue(self.controller.allowed())
        self.state.active = False
        self.assertFalse(self.controller.allowed())
        self.assertEqual(self.detector.calls, 0)

    def test_transport_cancel_does_not_extend_the_conversation_window(self):
        self.assertTrue(self.feed())
        self.now = 10
        self.controller.reset('transport')
        self.controller.reset('cancel')
        self.assertEqual(self.controller.deadline, 15)
        self.assertEqual(self.detector.resets[-2:], [False, False])

    def test_invalid_keyword_window_and_stale_generation_cannot_authorize(self):
        for word in ('贾维斯', 'unknown', [], None):
            self.detector.word = word
            self.assertFalse(self.feed())
        self.detector.word = '大黄蜂'
        self.assertFalse(self.controller.feed(np.zeros(320), 99, capture_nanos=0))
        self.assertFalse(self.state.awake)
        for window in (True, 0, 301, float('nan')):
            with self.assertRaises(ValueError):
                WakeController(self.hooks, self.detector, window_seconds=window)

    def test_reset_between_admission_and_native_entry_cannot_poison_new_stream(self):
        waiting, proceed = threading.Event(), threading.Event()
        real_lock = threading.RLock()
        results = []

        class DelayedLock:
            def __enter__(inner):
                if threading.current_thread().name == 'old-wake':
                    waiting.set()
                    if not proceed.wait(2):
                        raise TimeoutError('test native gate')
                real_lock.acquire()

            def __exit__(inner, *unused):
                real_lock.release()

        self.controller.native_lock = DelayedLock()
        thread = threading.Thread(
            target=lambda: results.append(self.feed()), name='old-wake', daemon=True
        )
        thread.start()
        try:
            self.assertTrue(waiting.wait(1))
            self.controller.reset('transport')
        finally:
            proceed.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [False])
        self.assertEqual(self.detector.calls, 0)
        self.assertFalse(self.state.awake)

    def test_stop_reset_and_native_feed_do_not_invert_session_lock(self):
        entered, release, stopping = threading.Event(), threading.Event(), threading.Event()
        native = threading.RLock()
        errors, results = [], []
        original_reset = self.detector.reset

        def feed(pcm):
            with native:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('test decode release')
                return '大黄蜂'

        def reset(**kwargs):
            with native:
                original_reset(**kwargs)

        def call():
            try:
                results.append(self.feed())
            except Exception as error:
                errors.append(type(error).__name__)

        def restart():
            try:
                with self.guard:
                    self.state.active = False
                    self.state.generation += 1
                    stopping.set()
                    self.controller.reset('stop')
                    self.state.active = True
                    self.state.generation += 1
                    self.controller.reset('start')
            except Exception as error:
                errors.append(type(error).__name__)

        self.detector.feed, self.detector.reset = feed, reset
        decoder = threading.Thread(target=call, daemon=True)
        stopper = threading.Thread(target=restart, daemon=True)
        decoder.start()
        try:
            self.assertTrue(entered.wait(1))
            stopper.start()
            self.assertTrue(stopping.wait(1))
        finally:
            release.set()
            decoder.join(2)
            if stopper.ident is not None:
                stopper.join(2)
        self.assertFalse(decoder.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [False])
        self.assertFalse(self.state.awake)
        self.assertEqual(self.touches, 0)


if __name__ == '__main__':
    unittest.main()

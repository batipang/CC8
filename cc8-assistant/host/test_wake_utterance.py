"""Local-only endpoint wake checks and current-capture admission."""

import threading
import unittest
from unittest.mock import Mock
import numpy as np
from dsp import FRAME, MAX_UTTERANCE_FRAMES
from wake_controller import WakeController, WakeHooks
from wake_utterance import WaitingUtterance, recheck_utterance


class WakeUtteranceTests(unittest.TestCase):
    def test_only_exact_local_keyword_and_owned_copy_are_used(self):
        source = np.linspace(-0.05, 0.05, FRAME * 3, dtype=np.float64)
        original = source.copy()
        for word, expected in (('Bumblebee', True), ('BubbleBeeSpaced', False), ('', False)):
            with self.subTest(word=word):
                retained = []

                def feed(frame):
                    retained.append(frame)
                    return word

                decoder = Mock(feed=feed)
                factory = Mock(return_value=decoder)
                self.assertIs(recheck_utterance(source, factory, lambda: True), expected)
                factory.assert_called_once_with()
                self.assertEqual(len(retained), 3)
                self.assertTrue(all(frame.dtype == np.float32 for frame in retained))
                self.assertTrue(all(not np.any(frame) for frame in retained))
                np.testing.assert_array_equal(source, original)

    def test_cancel_during_decode_never_confirms_an_already_seen_keyword(self):
        source = np.full(FRAME * 3, 0.1, dtype=np.float32)
        active = [True]
        frames = []

        def feed(frame):
            frames.append(frame)
            active[0] = False
            return 'Bumblebee'

        self.assertIsNone(recheck_utterance(source, lambda: Mock(feed=feed), lambda: active[0]))
        self.assertEqual(len(frames), 1)
        self.assertFalse(np.any(frames[0]))
        np.testing.assert_array_equal(source, np.full_like(source, 0.1))

    def test_invalid_or_cancelled_input_cannot_allocate_a_decoder(self):
        factory = Mock()
        for source in (
            np.zeros(0),
            np.zeros((2, FRAME)),
            np.full(FRAME, np.nan),
            np.full(FRAME, np.inf),
            np.zeros(FRAME + 1),
            np.zeros((MAX_UTTERANCE_FRAMES + 1) * FRAME),
        ):
            with self.subTest(shape=source.shape):
                self.assertIsNone(recheck_utterance(source, factory, lambda: True))
        self.assertIsNone(recheck_utterance(np.zeros(FRAME), factory, lambda: False))
        factory.assert_not_called()

    def test_decoder_error_erases_temporary_audio_without_modifying_utterance(self):
        source = np.ones(FRAME, dtype=np.float32)
        retained = []

        def feed(frame):
            retained.append(frame)
            raise RuntimeError('local native failure')

        with self.assertRaises(RuntimeError):
            recheck_utterance(source, lambda: Mock(feed=feed), lambda: True)
        self.assertFalse(np.any(retained[0]))
        np.testing.assert_array_equal(source, np.ones_like(source))

    def controller(self):
        state = dict(active=True, awake=False, generation=7, risk=False, minimum=100)
        detector = Mock()
        detector.utterance_recheck_supported = True
        metrics, events = [], []
        controller = None

        def awake(value):
            state['awake'] = value
            controller.awake_changed(value)

        hooks = WakeHooks(
            lock=threading.RLock(),
            active=lambda: state['active'],
            generation=lambda: state['generation'],
            awake=lambda: state['awake'],
            wake_mode=lambda: True,
            capture_current=lambda stamp: stamp >= state['minimum'],
            busy=lambda: False,
            set_awake=awake,
            touch_activity=Mock(),
            cancel=Mock(),
            status=Mock(),
            metric=lambda *a, **k: metrics.append((a, k)),
            log=lambda *a, **k: events.append((a, k)),
            render_risk=lambda: state['risk'],
        )
        controller = WakeController(hooks, detector, clock=lambda: 10)
        return controller, state, detector, metrics

    def test_confirmation_opens_window_once_and_does_not_give_prefix_to_next_turn(self):
        controller, state, detector, metrics = self.controller()
        ticket = controller.waiting_utterance(150)
        detector.recheck_utterance.return_value = True
        self.assertTrue(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
        self.assertTrue(state['awake'])
        self.assertFalse(controller.claim_prefix())
        self.assertEqual(controller.deadline, 70)
        self.assertIsNone(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
        detector.recheck_utterance.assert_called_once()
        self.assertEqual(sum(args[0] == 'local_wake_count' for args, _ in metrics), 1)

    def test_start_timestamp_render_risk_and_capture_stop_block_before_decode(self):
        for change in ('old_audio', 'playback', 'stop', 'generation', 'serial'):
            controller, state, detector, _ = self.controller()
            ticket = controller.waiting_utterance(150)
            if change == 'old_audio':
                state['minimum'] = 151
            elif change == 'playback':
                state['risk'] = True
            elif change == 'stop':
                state['active'] = False
            elif change == 'generation':
                state['generation'] += 1
            else:
                controller.reset('capture_gap')
            with self.subTest(change=change):
                self.assertIsNone(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
                detector.recheck_utterance.assert_not_called()
                self.assertFalse(state['awake'])

    def test_late_native_result_cannot_wake_after_owner_state_changes(self):
        for change in (
            'stop',
            'generation',
            'serial',
            'playback',
            'new_wake',
            'old_audio',
            'decoder',
        ):
            controller, state, detector, _ = self.controller()
            ticket = controller.waiting_utterance(150)

            def decode(*_):
                if change == 'stop':
                    state['active'] = False
                elif change == 'generation':
                    state['generation'] += 1
                elif change == 'serial':
                    controller.reset('capture_gap')
                elif change == 'playback':
                    state['risk'] = True
                elif change == 'new_wake':
                    state['awake'] = True
                elif change == 'old_audio':
                    state['minimum'] = 151
                else:
                    controller.detector = Mock()
                return True

            detector.recheck_utterance.side_effect = decode
            with self.subTest(change=change):
                self.assertIsNone(controller.recheck_utterance(ticket, np.zeros(FRAME), 7))
                self.assertEqual(state['awake'], change == 'new_wake')
                controller.hooks.touch_activity.assert_not_called()

    def test_unavailable_nonboolean_or_missed_recheck_never_opens_window(self):
        for answer in (False, None, 1, 'Bumblebee', RuntimeError('private')):
            controller, state, detector, _ = self.controller()
            ticket = controller.waiting_utterance(150)
            if isinstance(answer, Exception):
                detector.recheck_utterance.side_effect = answer
            else:
                detector.recheck_utterance.return_value = answer
            result = controller.recheck_utterance(ticket, np.zeros(FRAME), 7)
            self.assertIs(result, False if answer is False else None)
            self.assertFalse(state['awake'])

    def test_ticket_is_strictly_typed_and_initial_admission_is_current(self):
        controller, state, _, _ = self.controller()
        for stamp in (None, True, -1, 99):
            self.assertIsNone(controller.waiting_utterance(stamp))
        state['risk'] = True
        self.assertIsNone(controller.waiting_utterance(150))
        for values in ((True, 7, 150), (0, False, 150), (0, 7, -1)):
            with self.assertRaises(ValueError):
                WaitingUtterance(*values)
        for model in (True, -1, None, 1.0):
            with self.assertRaises(ValueError):
                WaitingUtterance(0, 7, 150, model_generation=model)


if __name__ == '__main__':
    unittest.main()

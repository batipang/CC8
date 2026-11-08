from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from bridge import Bridge
from chinese_wake_verifier import ChineseWakeVerifier, TARGET, WINDOW_FRAMES
from dsp import FRAME
from local_wake import WakeDetector
from wake_verifier_process import classify_text


class ChineseWakeVerifierTests(unittest.TestCase):
    def setUp(self):
        self.primary = np.full(FRAME, 0.1)
        self.alternate = np.full(FRAME, 0.2)
        self.spotter = Mock(**{'feed.return_value': '', 'compute_seconds': 0.0})
        self.recognize = Mock(return_value='')
        self.worker = Mock(ready=True, last_ready=True)
        self.worker.classify.side_effect = lambda pcm: classify_text(self.recognize(pcm))
        self.verifier = ChineseWakeVerifier(self.spotter, self.worker)

    def feed(self, hit=''):
        return self.verifier.feed(hit, self.primary, self.alternate)

    def test_primary_veto_requires_explicit_confusable_without_target(self):
        for text, expected in (
            ('请别打黄蜂', ''),
            ('大黃風箏', ''),
            ('大黃风筝', ''),
            ('別打黃蜂', ''),
            ('大黄蜂和大黃風箏', TARGET),
            ('别打黄蜂，大黄蜂请回答', TARGET),
            ('', TARGET),
            ('其他文本', TARGET),
        ):
            with self.subTest(text=text):
                self.recognize.return_value = text
                self.assertEqual(self.feed(TARGET), expected)

    def test_alternate_requires_target_without_confusable(self):
        self.spotter.feed.return_value = TARGET
        for text, expected in (
            ('打黄蜂', ''),
            ('大黄蜂和大黄风筝', ''),
            ('', ''),
            ('大黃蜂，请回答', TARGET),
        ):
            with self.subTest(text=text):
                self.recognize.return_value = text
                self.assertEqual(self.feed(), expected)

    def test_primary_has_priority_and_only_vetoed_primary_allows_second_decode(self):
        self.spotter.feed.return_value = TARGET
        self.recognize.side_effect = ('打黄蜂', '大黄蜂', '大黄蜂')
        self.assertEqual(self.feed(TARGET), TARGET)
        self.assertEqual(self.recognize.call_count, 2)
        self.assertEqual(self.feed(TARGET), TARGET)
        self.assertEqual(self.recognize.call_count, 3)

    def test_verifier_error_keeps_primary_and_rejects_alternate(self):
        self.spotter.feed.return_value = TARGET
        self.worker.classify.side_effect = None
        self.worker.classify.return_value = (False, False)
        self.assertEqual(self.feed(TARGET), TARGET)
        self.assertEqual(self.feed(), '')

    def test_unready_worker_keeps_primary_without_saving_or_decoding_input(self):
        self.feed()
        saved = self.verifier.primary_audio[0]
        self.worker.ready = False
        self.assertEqual(self.feed(TARGET), TARGET)
        self.assertFalse(np.any(saved))
        self.assertFalse(self.verifier.primary_audio)
        self.worker.classify.assert_not_called()

    def test_window_is_bounded_and_wipes_copies_on_eviction_reset_and_decode(self):
        self.feed()
        oldest = self.verifier.primary_audio[0]
        for _ in range(WINDOW_FRAMES):
            self.feed()
        self.assertFalse(np.any(oldest))
        self.assertEqual(len(self.verifier.primary_audio), WINDOW_FRAMES)
        first_alternate = self.verifier.alternate_audio[0]

        def recognize(pcm):
            self.assertEqual(len(pcm), WINDOW_FRAMES * FRAME)
            self.assertTrue(np.all(pcm == 0.1))
            return ''

        self.recognize.side_effect = recognize
        self.assertEqual(self.feed(TARGET), TARGET)
        self.assertFalse(np.any(self.recognize.call_args.args[0]))
        self.verifier.reset(clear_metrics=True)
        self.assertFalse(np.any(first_alternate))
        self.assertTrue(np.all(self.primary == 0.1))
        self.assertEqual(len(self.verifier.primary_audio), 0)
        self.assertEqual(self.verifier.decode_seconds, 0.0)

    def test_playback_or_input_gap_clears_history_and_preserves_standard_wake(self):
        self.feed()
        first = self.verifier.primary_audio[0]
        self.assertEqual(self.verifier.feed(TARGET, self.primary, None), TARGET)
        self.assertFalse(np.any(first))
        self.spotter.reset.assert_called_once_with(clear_metrics=False)
        self.recognize.assert_not_called()
        self.feed()
        self.spotter.feed.return_value = TARGET
        self.assertEqual(self.feed(), '')
        self.assertEqual(len(self.recognize.call_args.args[0]), 2 * FRAME)

    def test_wake_detector_clears_both_histories_on_english_or_chinese_admission(self):
        chinese = Mock(**{'feed.return_value': ''})
        english = Mock(**{'feed.return_value': ''})
        wake = WakeDetector(chinese, english, chinese_verifier=self.verifier)
        self.assertEqual(wake.feed(self.primary, alternate_pcm=self.alternate), '')
        english.feed.return_value = 'Bumblebee'
        self.assertEqual(wake.feed(self.primary, alternate_pcm=self.alternate), 'Bumblebee')
        self.assertEqual(len(self.verifier.primary_audio), 0)
        english.feed.return_value = ''
        chinese.feed.return_value = TARGET
        self.assertEqual(wake.feed(self.primary, alternate_pcm=self.alternate), TARGET)
        self.assertEqual(len(self.verifier.alternate_audio), 0)

    def test_optional_model_process_is_only_constructed_when_enabled_and_not_started_at_load(self):
        stop = SimpleNamespace(detector=object(), engine_lock=object())
        with (
            patch('local_wake.ChineseWake') as chinese,
            patch('local_wake.EnglishWake.load'),
            patch('wake_verifier_process.WakeVerifierProcess') as worker,
        ):
            self.assertIsNone(WakeDetector.load(stop).chinese_verifier)
            worker.assert_not_called()
            wake = WakeDetector.load(stop, verify_chinese=True)
            self.assertIsNotNone(wake.chinese_verifier)
            worker.assert_called_once_with()
            worker.return_value.start.assert_not_called()
            chinese.side_effect = [Mock(), RuntimeError('alternate stream unavailable')]
            self.assertIsNone(WakeDetector.load(stop, verify_chinese=True).chinese_verifier)

    def test_live_gate_ignores_alternate_on_echo_risk_and_discards_old_generations(self):
        wake = WakeDetector(
            Mock(**{'feed.return_value': ''}),
            Mock(**{'feed.return_value': ''}),
            chinese_verifier=self.verifier,
        )
        bridge = Bridge(None, None, wake_detector=wake)
        bridge.session.active = bridge.session.wake = True
        bridge.session.awake = False
        self.spotter.feed.return_value = TARGET
        self.recognize.return_value = TARGET
        for generation, risk in ((bridge.generation + 1, False), (bridge.generation, True)):
            self.assertFalse(
                bridge.wake.feed(
                    self.primary,
                    generation,
                    capture_nanos=0,
                    echo_risk=risk,
                    alternate_pcm=self.alternate,
                )
            )
        self.recognize.assert_not_called()
        self.assertFalse(bridge.session.awake)
        self.assertTrue(
            bridge.wake.feed(
                self.primary, bridge.generation, capture_nanos=1, alternate_pcm=self.alternate
            )
        )
        self.assertTrue(bridge.session.awake)
        self.assertFalse(self.verifier.alternate_audio)

    def test_complete_mode_verifies_weights_without_loading_or_starting_inference(self):
        stop = SimpleNamespace(detector=object(), engine_lock=object())
        with (
            patch('local_wake.ChineseWake'),
            patch('local_wake.EnglishWake.load'),
            patch('recognition.verify_model') as verify,
            patch('complete_wake_process.CompleteWakeVerifierProcess') as worker,
        ):
            wake = WakeDetector.load(stop, verify_chinese_complete=True)
            self.assertTrue(wake.complete_chinese_enabled)
            self.assertIsNone(wake.chinese_verifier)
            verify.assert_called_once_with()
            worker.assert_called_once_with()
            worker.return_value.start.assert_not_called()
            wake.start_capture()
            worker.return_value.start.assert_called_once_with()
            wake.stop_capture()
            worker.return_value.close.assert_called_once_with()
            with self.assertRaises(ValueError):
                WakeDetector.load(stop, verify_chinese=True, verify_chinese_complete=True)

    def test_transport_rebind_starts_worker_only_for_the_existing_capture(self):
        wake = WakeDetector(Mock(), Mock(), chinese_verifier=self.verifier)
        bridge = Bridge(None, None, wake_detector=wake)
        bridge.session.active = True
        bridge.wake.reset('start')
        self.worker.start.assert_called_once_with()
        bridge.wake.reset('cancel')
        self.worker.close.assert_not_called()
        bridge.wake.reset('transport')
        self.worker.close.assert_called_once_with()
        bridge.session.capture_start_ack = True
        bridge.session.rebind({'epoch': bridge.session.epoch, 'capture_id': 'obsolete'})
        self.worker.start.assert_called_once_with()
        bridge.session.capture_start_ack = False
        bridge.session.rebind({'epoch': bridge.session.epoch})
        self.assertEqual(self.worker.start.call_count, 2)
        bridge.session.rebind({'epoch': bridge.session.epoch})
        self.assertEqual(self.worker.start.call_count, 2)
        bridge.session.deactivate()
        bridge.wake.reset('cancel')
        self.assertEqual(self.worker.close.call_count, 2)
        bridge.wake.reset('rebind')
        self.assertEqual(self.worker.start.call_count, 2)

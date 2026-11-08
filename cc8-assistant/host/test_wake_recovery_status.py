"""Waiting-frame feedback across independent local model recovery states."""

from dataclasses import replace
import unittest

import test_wake_controller as fixtures
from wake_controller import RECOVERING, WAITING

ENGLISH_RECOVERING = '英文唤醒增强正在恢复，基础英文唤醒仍可用'
ENGLISH_RECOVERY_FAILED = '英文唤醒增强暂不可用，基础英文唤醒仍可用；可暂停语音后重新开始'


class RecoveryStatusTests(unittest.TestCase):
    def owner(self):
        f = fixtures.WakeControllerTests()
        f.setUp()
        f.detector.word = ''
        f.detector.complete_english_enabled = True
        f.detector.english_recovery_generation = 2
        f.detector.english_recovery_snapshot = {'state': 'retry_wait'}
        return f

    def test_optional_english_failure_is_visible_and_cleared_only_after_its_recovery(self):
        f = self.owner()
        f.detector.complete_recovery_snapshot = {'state': 'ready'}
        f.feed()
        self.assertEqual(f.statuses, [('error', ENGLISH_RECOVERING)])
        f.feed()
        self.assertEqual(len(f.statuses), 1)
        f.detector.english_recovery_snapshot = {'state': 'failed'}
        f.feed()
        self.assertEqual(f.statuses[-1], ('error', ENGLISH_RECOVERY_FAILED))
        f.detector.english_recovery_snapshot = {'state': 'ready'}
        f.feed()
        f.feed()
        self.assertEqual(f.statuses[-1], ('listening', WAITING))
        self.assertEqual(len(f.statuses), 3)
        self.assertEqual(f.touches, 0)
        self.assertFalse(f.state.awake)
        self.assertEqual(f.cancelled, [])

    def test_chinese_recovery_has_priority_and_does_not_hide_remaining_english_failure(self):
        f = self.owner()
        f.detector.complete_recovery_snapshot = {'state': 'retry_wait'}
        f.detector.english_recovery_snapshot = {'state': 'failed'}
        f.feed()
        self.assertEqual(f.statuses, [('error', RECOVERING)])
        f.detector.english_recovery_generation += 1
        f.feed()
        self.assertEqual(len(f.statuses), 1)
        f.detector.complete_recovery_snapshot = {'state': 'ready'}
        f.feed()
        self.assertEqual(f.statuses[-1], ('error', ENGLISH_RECOVERY_FAILED))
        self.assertNotIn(('listening', WAITING), f.statuses)

    def test_busy_render_awake_and_stale_frames_cannot_replace_current_feedback(self):
        for reason in ('busy', 'render', 'awake', 'inactive', 'generation'):
            with self.subTest(reason=reason):
                f = self.owner()
                if reason == 'busy':
                    f.state.busy = True
                elif reason == 'render':
                    f.controller.hooks = replace(f.hooks, render_risk=lambda: True)
                elif reason == 'awake':
                    f.state.awake = True
                elif reason == 'inactive':
                    f.state.active = False
                else:
                    f.controller.feed([], 999, capture_nanos=0)
                    self.assertEqual(f.statuses, [])
                    continue
                f.feed()
                self.assertEqual(f.statuses, [])

    def test_disabled_stopped_and_legacy_english_models_have_no_recovery_message(self):
        for reason in ('disabled', 'stopped', 'legacy'):
            with self.subTest(reason=reason):
                f = self.owner()
                if reason == 'disabled':
                    f.detector.complete_english_enabled = False
                elif reason == 'stopped':
                    f.detector.english_recovery_snapshot = {'state': 'stopped'}
                else:
                    del f.detector.english_recovery_snapshot
                f.feed()
                self.assertEqual(f.statuses, [])


if __name__ == '__main__':
    unittest.main()

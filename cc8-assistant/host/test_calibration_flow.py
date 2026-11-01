"""Run complete self-check outcomes with a synthetic clock and silent providers."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bridge import Bridge


class CalibrationFlowTests(unittest.TestCase):
    def session(self):
        with patch('threading.Thread.start'):
            session = Bridge(None, None)
        session.active = True
        session.connection = object()
        session.event = Mock()
        session.log = Mock()
        session.save_status = Mock()
        session.speak = Mock(return_value=True)
        return session

    def run_check(self, session):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
            self.assertLess(now[0], 41, 'self-check exceeded its bounded drain window')

        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
        with patch('calibration_controller.time', clock):
            session.check_echo(session.generation)

    def test_quiet_check_finishes_and_preserves_existing_microphone(self):
        session = self.session()
        self.run_check(session)
        report = session.metrics['echo_self_check']
        self.assertEqual(report['state'], 'passed')
        self.assertGreaterEqual(report['seconds'], 20)
        self.assertEqual(session.speak.call_count, 5)
        self.assertTrue(session.active)
        self.assertFalse(session.calibration.running)
        self.assertFalse(session.calibration.failed)

    def test_candidate_or_unfinished_recognition_cannot_pass(self):
        for unfinished in (False, True):
            with self.subTest(unfinished=unfinished):
                session = self.session()
                session.recognition.busy = unfinished

                def speak(*unused):
                    if not unfinished:
                        session.metrics['echo_self_check']['unmatched_candidates'] += 1
                    return True

                session.speak.side_effect = speak
                self.run_check(session)
                self.assertEqual(
                    session.metrics['echo_self_check']['state'],
                    'incomplete' if unfinished else 'failed',
                )
                self.assertFalse(session.calibration.running)
                self.assertTrue(session.calibration.failed)
                self.assertTrue(session.active)

    def test_cancelled_playback_finishes_report_without_starting_more_speech(self):
        for invalidate in (False, True):
            with self.subTest(invalidate=invalidate):
                session = self.session()

                def speak(*unused):
                    if invalidate:
                        session.generation += 1
                    return False

                session.speak.side_effect = speak
                self.run_check(session)
                self.assertEqual(session.metrics['echo_self_check']['state'], 'interrupted')
                self.assertEqual(session.speak.call_count, 1)
                self.assertFalse(session.calibration.running)
                self.assertTrue(session.active)

    def test_output_exception_records_failure_and_releases_running_state(self):
        session = self.session()
        session.speak.side_effect = EOFError('synthetic disconnect')
        with self.assertRaises(EOFError):
            self.run_check(session)
        self.assertEqual(session.metrics['echo_self_check']['state'], 'error')
        self.assertTrue(session.calibration.failed)
        self.assertFalse(session.calibration.running)
        self.assertTrue(session.active)
        self.assertEqual(session.save_status.call_count, 2)


if __name__ == '__main__':
    unittest.main()

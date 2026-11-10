"""Actual playback-driven pose; no capture, models, device output or retained PCM."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.java_toolchain import require_java

ROOT = Path(__file__).resolve().parents[1] / 'app/src'


class SpeechMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = require_java()
        cls.folder = tempfile.TemporaryDirectory(prefix='cc8-speech-motion-')
        cls.addClassCleanup(cls.folder.cleanup)
        files = [
            ROOT / 'main/java/com/happyhome/cc8assistant/SpeechMotion.java',
            ROOT / 'test/java/com/happyhome/cc8assistant/SpeechMotionCheck.java',
        ]
        built = subprocess.run(
            [
                str(cls.java / 'javac'),
                '-encoding',
                'UTF-8',
                '-d',
                cls.folder.name,
                *map(str, files),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if built.returncode:
            raise AssertionError(built.stderr)

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                self.folder.name,
                'com.happyhome.cc8assistant.SpeechMotionCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name + ' passed')

    def test_live_articulation_uses_played_time_with_smooth_attack_and_release(self):
        self.scenario('smoothing')

    def test_pause_stop_empty_and_new_owner_close_without_trailing_motion(self):
        self.scenario('stopped')

    def test_ui_refresh_and_regressed_clock_cannot_animate_future_audio(self):
        self.scenario('frozen')

    def test_invalid_signal_or_owner_cannot_open_mouth(self):
        self.scenario('invalid')

    def test_fixed_seed_long_session_keeps_pose_bounded(self):
        self.scenario('bounded')


if __name__ == '__main__':
    unittest.main()

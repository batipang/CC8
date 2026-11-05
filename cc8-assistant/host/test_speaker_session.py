"""Run actual session output ownership code with concurrent Java threads, never device audio."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.java_toolchain import require_java

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src'


class SpeakerSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = require_java()
        cls.temp = tempfile.TemporaryDirectory(prefix='cc8-speaker-')
        cls.addClassCleanup(cls.temp.cleanup)
        files = [
            JAVA / 'main/java/com/happyhome/cc8assistant/SpeakerSession.java',
            JAVA / 'test/java/com/happyhome/cc8assistant/SpeakerSessionCheck.java',
        ]
        result = subprocess.run(
            [str(cls.java / 'javac'), '-encoding', 'UTF-8', '-d', cls.temp.name, *map(str, files)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr)

    def scenario(self, name):
        result = subprocess.run(
            [
                str(self.java / 'java'),
                '-cp',
                self.temp.name,
                'com.happyhome.cc8assistant.SpeakerSessionCheck',
                name,
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name + ' passed')

    def test_warm_handover_preserves_one_track_and_clause_local_heads(self):
        self.scenario('handover')

    def test_cancel_flushes_again_after_late_write_before_new_owner(self):
        self.scenario('late_write')

    def test_pause_keeps_lease_without_idle_writes_or_reset(self):
        self.scenario('pause')

    def test_close_during_native_construction_never_plays_obsolete_track(self):
        self.scenario('construct_close')

    def test_lifetime_loss_during_construction_never_plays_obsolete_track(self):
        self.scenario('construct_revoke')

    def test_close_waits_for_native_write_before_single_release(self):
        self.scenario('close_write')

    def test_native_negative_write_closes_resource_and_rejects_reuse(self):
        self.scenario('negative')

    def test_native_oversize_write_closes_resource_and_rejects_reuse(self):
        self.scenario('oversize')

    def test_native_write_exception_closes_resource_and_rejects_reuse(self):
        self.scenario('throw_write')

    def test_zero_and_short_writes_advance_only_accepted_physical_frames(self):
        self.scenario('short_zero')

    def test_pause_failure_cannot_leave_idle_audio_running(self):
        self.scenario('pause_error')

    def test_resume_failure_cannot_leave_idle_audio_running(self):
        self.scenario('resume_error')

    def test_flush_failure_cannot_resume_old_speech(self):
        self.scenario('cancel_error')

    def test_idle_clock_closes_on_lifetime_loss_without_any_current_player(self):
        self.scenario('idle_revoke')

    def test_low_32_bit_head_unwraps_near_current_physical_frame_counter(self):
        self.scenario('wrap')

    def test_idle_write_failure_releases_track_and_rejects_acquisition(self):
        self.scenario('idle_failure')

    def test_short_idle_writes_warm_only_the_actual_accepted_frames(self):
        self.scenario('short_warm')

    def test_revoked_waiter_does_not_steal_or_block_next_clause(self):
        self.scenario('revoked_claimant')

    def test_native_initialization_failure_is_reported_to_acquirer(self):
        self.scenario('initial_failure')

    def test_close_during_idle_write_waits_before_releasing(self):
        self.scenario('idle_close')

    def test_revoked_clause_is_cleared_before_new_caller_acquires(self):
        self.scenario('caller_loss')

    def test_control_failure_preserves_outstanding_write_until_release_is_safe(self):
        self.scenario('control_write_error')

    def test_interrupted_waiter_releases_its_idle_priority_and_allows_fresh_clause(self):
        self.scenario('interrupted_claimant')


if __name__ == '__main__':
    unittest.main()

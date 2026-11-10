"""Actual Player, output adapter and lifetime helpers; no device, cloud or audio capture."""

import unittest

from speaker_harness import SpeakerHarness


class SpeakerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = SpeakerHarness()
        cls.addClassCleanup(cls.fixture.close)

    def check(self, scenario):
        result = self.fixture.run(scenario)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), scenario + ' passed')

    def test_real_player_emits_local_reference_receipts_and_absolute_clock_projection(self):
        self.check('accounting')

    def test_next_clause_can_start_before_old_finally_without_reset_or_release(self):
        self.check('handover')

    def test_stop_then_late_playback_cannot_reopen_output_but_explicit_task_can(self):
        self.check('stopped')

    def test_standalone_task_replacement_and_three_terminal_paths_close_only_their_owner(self):
        self.check('tasks')

    def test_task_completion_and_interrupt_keep_the_existing_voice_clock_until_stop(self):
        self.check('voice_task')

    def test_min_sdk_legacy_output_fallback_handles_short_writes(self):
        self.check('legacy')

    def test_uninitialized_native_track_is_released_and_reports_one_error(self):
        self.check('invalid')

    def test_native_volume_setup_failure_releases_the_created_track(self):
        self.check('volume')

    def test_invalid_native_minimum_buffer_never_reports_playback_completion(self):
        self.check('minimum')

    def test_old_route_controls_and_pcm_cannot_touch_new_same_generation_player(self):
        self.check('route')

    def test_native_volume_error_result_releases_the_track_instead_of_using_default_gain(self):
        self.check('volume_result')

    def test_desktop_photo_keeps_audio_closed_while_explicit_scene_analysis_gets_output(self):
        self.check('vision_output')

    def test_worker_observing_stop_cannot_consume_the_explicit_native_stop_receipt(self):
        # The added latch checkpoint forces the worker to observe stopped before cancel returns.
        # The ordinary fixture above still compiles the unmodified complete Player.
        fixture = SpeakerHarness(cancel_interleave=True)
        try:
            result = fixture.run('visuals')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), 'visuals passed')
        finally:
            fixture.close()

    def test_mouth_stays_closed_while_warm_samples_are_still_ahead_of_content(self):
        self.check('prefix_visuals')

    def test_shape_uses_accepted_physical_audio_and_closes_on_pause_and_cancel(self):
        self.check('avatar_motion')

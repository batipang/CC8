"""Synthetic PCM checks measure gate behavior, not real-world ASR accuracy."""

from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

import numpy as np

from dsp import FRAME, NearSpeechDetector
from recognition_environment import profile
import test_voice_preferences as settings_fixture


class SpeechVad:
    def is_speech(self, data, rate):
        return True


class RecognitionEnvironmentTests(unittest.TestCase):
    def pcm(self, dbfs):
        # Fixed alternating samples have exact RMS and no privileged input.
        return np.resize(np.array([-1.0, 1.0], dtype=np.float32), FRAME) * 10 ** (dbfs / 20)

    def feed(self, detector, dbfs, count=24):
        pcm = self.pcm(dbfs)
        zero = np.zeros(FRAME, dtype=np.float32)
        return [detector.feed(pcm, pcm, zero) for _ in range(count)]

    def test_normal_preserves_deployed_floor_margin_and_vote_boundaries(self):
        value = profile('normal')
        self.assertEqual(
            (
                value.minimum_dbfs,
                value.maximum_dbfs,
                value.noise_margin_db,
                value.confirmation_frames,
                value.confirmation_votes,
            ),
            (-43, -34, 12, 20, 16),
        )
        detector = NearSpeechDetector(SpeechVad())
        self.assertFalse(any(credible for _, credible in self.feed(detector, -44)))
        detector.set_environment('normal')
        readings = self.feed(detector, -41)
        self.assertFalse(any(credible for _, credible in readings[:19]))
        self.assertTrue(readings[19][1])

    def test_quiet_accepts_lower_energy_but_keeps_confirmation_and_echo_guards(self):
        detector = NearSpeechDetector(SpeechVad())
        self.assertFalse(any(voiced for voiced, _ in self.feed(detector, -45)))
        detector.set_environment('quiet')
        readings = self.feed(detector, -45)
        self.assertTrue(readings[-1][1])
        self.assertFalse(any(credible for _, credible in readings[:19]))
        reference = self.pcm(-25)
        residual = reference * 0.10
        detector.set_environment('quiet')
        for index in range(24):
            voiced, credible = detector.feed(residual, reference, reference)
            self.assertFalse(credible)
            if index >= 1:
                self.assertFalse(voiced)

    def test_television_is_stricter_and_restore_drops_all_old_votes(self):
        detector = NearSpeechDetector(SpeechVad())
        self.assertTrue(self.feed(detector, -41)[-1][1])
        detector.set_environment('television')
        self.assertFalse(any(voiced for voiced, _ in self.feed(detector, -41)))
        detector.set_environment('television')
        readings = self.feed(detector, -30)
        self.assertFalse(any(credible for _, credible in readings[:23]))
        self.assertTrue(readings[23][1])
        detector.set_environment('normal')
        self.assertEqual(len(detector.votes), 0)
        self.assertEqual(detector.noise, -58)
        self.assertFalse(detector.credible)
        self.assertTrue(self.feed(detector, -41)[-1][1])

    def test_unknown_names_cannot_mutate_state_and_presets_are_immutable(self):
        detector = NearSpeechDetector(SpeechVad())
        self.feed(detector, -30)
        votes = list(detector.votes)
        for value in ('auto', '', None, True, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                detector.set_environment(value)
            self.assertEqual(list(detector.votes), votes)
        with self.assertRaises(FrozenInstanceError):
            profile('normal').minimum_dbfs = -100


class EnvironmentSettingTests(unittest.TestCase):
    def test_explicit_idle_apply_restore_and_revision_leave_capture_and_provider_unchanged(self):
        bridge = settings_fixture.VoicePreferencesTests().session()
        generation = bridge.generation
        for revision, name in enumerate(('quiet', 'television', 'normal')):
            bridge.on_event(dict(event='environment_profile_set', revision=revision, profile=name))
            state = bridge.session.voice_preferences_state()
            self.assertEqual(state['environment_profile'], name)
            self.assertEqual(state['revision'], revision + 1)
            self.assertEqual(bridge.capture.endpoint.near_speech.environment.name, name)
            self.assertTrue(bridge.events[-1][1]['ok'])
        self.assertFalse(bridge.session.active)
        self.assertEqual(bridge.generation, generation)
        self.assertEqual(bridge.speech.rates, [])
        self.assertEqual(bridge.workspace.tasks, [])
        self.assertTrue(bridge.response.queue.empty())
        self.assertTrue(bridge.recognition.queue.empty())

    def test_stale_busy_and_pending_pcm_cannot_change_live_thresholds(self):
        for owner, field in (
            ('session', 'active'),
            ('response', 'busy'),
            ('recognition', 'busy'),
            ('calibration', 'active'),
            ('playback', 'playing'),
        ):
            bridge = settings_fixture.VoicePreferencesTests().session()
            setattr(getattr(bridge, owner), field, True)
            bridge.on_event(dict(event='environment_profile_set', revision=0, profile='quiet'))
            self.assertFalse(bridge.events[-1][1]['ok'])
            self.assertEqual(bridge.capture.endpoint.near_speech.environment.name, 'normal')
            self.assertEqual(bridge.session.voice_preferences.revision, 0)
        bridge = settings_fixture.VoicePreferencesTests().session()
        bridge.on_event(dict(event='environment_profile_set', revision=1, profile='quiet'))
        self.assertFalse(bridge.events[-1][1]['ok'])
        bridge.capture.endpoint.vad.talking = True
        with self.assertRaises(RuntimeError):
            bridge.capture.endpoint.set_environment('quiet')
        self.assertEqual(bridge.capture.endpoint.near_speech.environment.name, 'normal')

    def test_reset_failure_cannot_publish_an_applied_profile(self):
        bridge = settings_fixture.VoicePreferencesTests().session()
        with patch.object(
            bridge.capture.endpoint,
            'reset_voice_activity',
            side_effect=RuntimeError('inert fixture'),
        ):
            with self.assertRaises(RuntimeError):
                bridge.on_event(dict(event='environment_profile_set', revision=0, profile='quiet'))
        self.assertEqual(bridge.capture.endpoint.near_speech.environment.name, 'normal')
        self.assertEqual(bridge.session.voice_preferences.environment_profile, 'normal')
        self.assertEqual(bridge.session.voice_preferences.revision, 0)

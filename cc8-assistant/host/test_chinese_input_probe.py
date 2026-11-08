"""Short public Chinese probe, independent stream replay and receipt integrity."""

from copy import deepcopy
import hashlib
import unittest
from unittest.mock import Mock, patch
import numpy as np

from dsp import FRAME
from local_wake import replay_chinese_inputs
from wake_input_probe import WakeInputProbe
from wake_input_receipt import (
    CHINESE_QA_FRAMES,
    CHINESE_QA_PCM_SHA256,
    CHINESE_QA_WAV_SHA256,
    CHINESE_QA_PROFILE,
    summarize,
    valid_request,
)
import test_wake_input_probe as fixtures


class ChineseInputProbeTests(unittest.TestCase):
    def fixture(self):
        source = np.random.default_rng(953).integers(-4000, 4000, CHINESE_QA_FRAMES, dtype='<i2')
        pcm = source.tobytes()
        with patch('wake_input_probe.CHINESE_QA_PCM_SHA256', hashlib.sha256(pcm).hexdigest()):
            probe = WakeInputProbe(pcm, chinese=True, timer=Mock())
        audio = np.zeros(51200)
        audio[3200 : 3200 + len(source)] = source.astype(float) / 32768
        return probe, audio

    def test_short_fixture_requires_the_whole_pinned_identity(self):
        meta = {
            **fixtures.metadata(),
            'frames': CHINESE_QA_FRAMES,
            'pcm_sha256': CHINESE_QA_PCM_SHA256,
            'wav_sha256': CHINESE_QA_WAV_SHA256,
        }
        request = {
            'operation': 'wake_input_arm',
            'directory': 'cc8-public-speech-abcdefgh',
            'metadata': meta,
        }
        self.assertTrue(valid_request(request))
        for field, value in (
            ('frames', CHINESE_QA_FRAMES + 1),
            ('pcm_sha256', 'a' * 64),
            ('wav_sha256', 'a' * 64),
        ):
            self.assertFalse(valid_request({**request, 'metadata': {**meta, field: value}}))
        with self.assertRaises(ValueError):
            WakeInputProbe(bytes(CHINESE_QA_FRAMES * 2), chinese=True, timer=Mock())

    def test_cancel_and_bad_frames_erase_short_probe_without_any_decode(self):
        for bad_frame in (False, True):
            probe, _ = self.fixture()
            retained = probe.reference, probe.primary, probe.alternate, probe.raw
            probe.observe(np.ones(FRAME), None, np.ones(FRAME))
            if bad_frame:
                probe.observe(np.ones(17), None)
            else:
                probe.cancel()
            with patch('local_wake.replay_chinese_inputs') as decode:
                self.assertEqual(probe.read(), {'status': 'unavailable'})
            decode.assert_not_called()
            self.assertTrue(all(not np.any(part) for part in retained))

    def test_chinese_fresh_replay_cancellation_never_feeds_the_next_frame(self):
        with (
            patch('local_wake.keyword_spotter') as loader,
            patch('local_wake.ChineseWake') as factory,
        ):
            self.assertEqual(
                replay_chinese_inputs({'raw': np.ones(FRAME)}, lambda: True),
                {'status': 'unavailable'},
            )
            loader.assert_not_called()
            cancelled = Mock(side_effect=(False, True))
            self.assertEqual(
                replay_chinese_inputs({'raw': np.ones(FRAME)}, cancelled), {'status': 'unavailable'}
            )
            factory.return_value.feed.assert_not_called()

    def test_each_matched_stream_is_replayed_without_waiting_for_the_other_two(self):
        for selected in ('primary', 'alternate', 'raw'):
            for failed in (False, True):
                with self.subTest(selected=selected, failed=failed):
                    probe, audio = self.fixture()
                    retained = probe.reference, probe.primary, probe.alternate, probe.raw
                    for frame in audio.reshape(-1, FRAME):
                        streams = {
                            key: frame if key == selected else np.zeros(FRAME)
                            for key in ('primary', 'alternate', 'raw')
                        }
                        probe.observe(
                            streams['primary'],
                            streams['alternate'],
                            streams['raw'],
                            stream_samples=14400000,
                        )

                    def replay(streams, cancelled):
                        self.assertEqual(set(streams), {selected})
                        np.testing.assert_array_equal(streams[selected], audio)
                        self.assertFalse(cancelled())
                        if failed:
                            raise RuntimeError('private native detail')
                        return {'status': 'measured', selected + '_hit': False}

                    with patch('local_wake.replay_chinese_inputs', side_effect=replay) as decode:
                        report = probe.read()
                    decode.assert_called_once()
                    self.assertEqual(report['fixture'], CHINESE_QA_PROFILE)
                    self.assertEqual(report['live_decoder'], {'primary_samples_before': 14400000})
                    self.assertEqual(report['fresh_replay'], {'status': 'not_run'})
                    for key, value in report['individual_replay'].items():
                        self.assertEqual(
                            value,
                            (
                                {'status': 'unavailable'}
                                if failed
                                else {'status': 'measured', 'keyword_hit': False}
                            )
                            if key == selected
                            else {'status': 'not_run'},
                        )
                    self.assertEqual(summarize(report), report)
                    self.assertTrue(all(not np.any(part) for part in retained))
                    self.assertEqual(probe.read(), {'status': 'unavailable'})
                    self.assertNotIn('private native detail', str(report))

    def matched_report(self):
        probe, audio = self.fixture()
        for frame in audio.reshape(-1, FRAME):
            probe.observe(frame, -frame, frame / 2, stream_samples=320)
        with patch(
            'local_wake.replay_chinese_inputs',
            return_value={
                'status': 'measured',
                'primary_hit': True,
                'alternate_hit': False,
                'raw_hit': True,
            },
        ) as replay:
            report = probe.read()
        replay.assert_called_once()
        return report

    def test_all_matched_streams_decode_once_and_aggregate_cannot_disagree(self):
        report = self.matched_report()
        self.assertEqual(summarize(report), report)
        invalid = deepcopy(report)
        invalid['individual_replay']['primary']['keyword_hit'] = False
        self.assertEqual(summarize(invalid), {'status': 'invalid'})
        invalid = deepcopy(report)
        invalid['individual_replay']['raw']['keyword_hit'] = 1
        self.assertEqual(summarize(invalid), {'status': 'invalid'})
        invalid = deepcopy(report)
        invalid['individual_replay']['raw']['audio'] = 'private'
        self.assertEqual(summarize(invalid), {'status': 'invalid'})
        invalid = deepcopy(report)
        invalid['fixture'] = 'unknown'
        self.assertEqual(summarize(invalid), {'status': 'invalid'})
        for key, value in (
            ('primary_samples_before', True),
            ('primary_samples_before', -1),
            ('primary_samples_before', 2**53),
            ('pcm', 'private'),
        ):
            invalid = deepcopy(report)
            invalid['live_decoder'][key] = value
            self.assertEqual(summarize(invalid), {'status': 'invalid'})

    def test_gap_cannot_be_compacted_replayed_or_reported_as_full_match(self):
        probe, audio = self.fixture()
        retained = probe.raw
        for index, frame in enumerate(audio.reshape(-1, FRAME)):
            probe.observe(frame, frame, frame if index else None)
        with patch(
            'local_wake.replay_chinese_inputs',
            return_value={'status': 'measured', 'primary_hit': True, 'alternate_hit': True},
        ) as decode:
            report = probe.read()
        self.assertEqual(set(decode.call_args.args[0]), {'primary', 'alternate'})
        self.assertEqual(report['raw'], {'status': 'input_gap'})
        self.assertEqual(report['individual_replay']['raw'], {'status': 'not_run'})
        self.assertEqual(summarize(report), report)
        self.assertFalse(np.any(retained))
        invalid = deepcopy(report)
        invalid['raw'] = deepcopy(report['primary'])
        self.assertEqual(summarize(invalid), {'status': 'invalid'})
        invalid = deepcopy(report)
        invalid['individual_replay']['raw'] = {'status': 'measured', 'keyword_hit': True}
        self.assertEqual(summarize(invalid), {'status': 'invalid'})

    def test_native_chinese_position_is_observed_before_the_first_feed(self):
        bridge, detector = fixtures.WakeInputTests().bridge()
        probe, _ = self.fixture()
        bridge.wake.input_probe = probe
        detector.chinese_stream_samples = 14400000

        def feed(*_, **__):
            detector.chinese_stream_samples = 14400160
            return ''

        detector.feed.side_effect = feed
        bridge.wake.feed(
            np.zeros(FRAME), bridge.generation, capture_nanos=0, alternate_pcm=np.zeros(FRAME)
        )
        receipt = bridge.wake.read_input_probe(probe.token)
        self.assertEqual(receipt['live_decoder'], {'primary_samples_before': 14400000})
        self.assertEqual(summarize(receipt), receipt)

    def test_short_profile_dispatch_uses_the_existing_chinese_ownership_path(self):
        bridge, detector = fixtures.WakeInputTests().bridge()
        self.assertEqual(bridge.health()['chinese_wake_input_probe_version'], 2)
        detector.start_capture.assert_not_called()
        bridge.wake.arm_input_probe = Mock(return_value={'status': 'armed'})
        with patch('known_speech_evidence.load_fixture', return_value=b'fixed-public-fixture'):
            bridge.probes.arm_wake_input_probe(None, None, {'pcm_sha256': CHINESE_QA_PCM_SHA256})
        bridge.wake.arm_input_probe.assert_called_once_with(b'fixed-public-fixture', chinese=True)


if __name__ == '__main__':
    unittest.main()

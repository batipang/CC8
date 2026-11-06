"""Privacy, input ownership and comparison regressions for the wake probe."""

import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch

import numpy as np

from bridge import Bridge
from announce import AnnouncementServer
from dsp import FRAME
from local_wake import replay_english_inputs
from wake_input_probe import NAME_END, WakeInputProbe, compare_stream
from wake_input_receipt import (
    FIXTURE_FRAMES,
    FIXTURE_PCM_SHA256,
    FIXTURE_WAV_SHA256,
    summarize,
    valid_request,
)


def metadata():
    return {
        'schema': 1,
        'sample_rate': 16000,
        'channels': 1,
        'sample_width_bytes': 2,
        'frames': FIXTURE_FRAMES,
        'pcm_sha256': FIXTURE_PCM_SHA256,
        'wav_sha256': FIXTURE_WAV_SHA256,
        'acoustic_playback_confirmed': False,
    }


def synthesize():
    rng = np.random.default_rng(887)
    source = rng.integers(-4000, 4000, FIXTURE_FRAMES, dtype='<i2')
    return source.tobytes(), source.astype(float) / 32768


def fixture_probe(pcm):
    with patch('wake_input_probe.FIXTURE_PCM_SHA256', hashlib.sha256(pcm).hexdigest()):
        return WakeInputProbe(pcm, timer=Mock())


class WakeInputTests(unittest.TestCase):
    def test_only_pinned_fixture_and_token_requests_allowed(self):
        arm = {
            'operation': 'wake_input_arm',
            'directory': 'cc8-public-speech-abcdefgh',
            'metadata': metadata(),
        }
        self.assertTrue(valid_request(arm))
        for bad in (
            {**arm, 'text': 'unexpected'},
            {**arm, 'directory': '../cc8-public-speech-abcdefgh'},
            {**arm, 'metadata': {**metadata(), 'frames': FIXTURE_FRAMES + 1}},
            {**arm, 'metadata': {**metadata(), 'pcm_sha256': 'a' * 64}},
        ):
            self.assertFalse(valid_request(bad))
        self.assertTrue(valid_request({'operation': 'wake_input_result', 'token': 'a' * 32}))
        self.assertFalse(
            valid_request({'operation': 'wake_input_result', 'token': 'a' * 32, 'audio': True})
        )

    def test_owner_socket_uses_private_fixture_root_and_returns_only_probe_receipt(self):
        bridge = Mock()
        server = AnnouncementServer(bridge, Path('/owner/private/announce.sock'))
        bridge.probes.arm_wake_input_probe.return_value = {'status': 'armed', 'token': 'a' * 32}
        bridge.wake.read_input_probe.return_value = {'status': 'unavailable'}
        arm = {
            'operation': 'wake_input_arm',
            'directory': 'cc8-public-speech-abcdefgh',
            'metadata': metadata(),
        }
        for payload, expected in (
            ({**arm, 'directory': '../fixture'}, 'invalid_request'),
            (arm, 'armed'),
            ({'operation': 'wake_input_result', 'token': 'a' * 32}, 'unavailable'),
        ):
            reader, writer = socket.socketpair()
            with writer:
                self.assertTrue(server.slots.acquire(blocking=False))
                writer.sendall((json.dumps(payload) + '\n').encode())
                server.handle(reader)
                self.assertEqual(json.loads(writer.recv(4096))['status'], expected)
        bridge.probes.arm_wake_input_probe.assert_called_once_with(
            Path('/owner/private/public-speech-fixtures'), arm['directory'], arm['metadata']
        )
        bridge.wake.read_input_probe.assert_called_once_with('a' * 32)
        bridge.announce.assert_not_called()
        bridge.control_device.assert_not_called()

    def test_question_anchor_does_not_substitute_for_name_evidence(self):
        _, source = synthesize()
        start = 3200
        received = np.zeros(51200)
        received[start : start + len(source)] = source / 2
        self.assertEqual(compare_stream(received, source)['status'], 'matched')
        received[start : start + NAME_END] = 0
        self.assertEqual(
            compare_stream(received, source)['status'], 'name_or_question_shape_unconfirmed'
        )
        self.assertEqual(
            compare_stream(np.zeros_like(received), source)['status'], 'question_unconfirmed'
        )
        self.assertEqual(compare_stream(received[:100], source), {'status': 'capture_incomplete'})

    def test_one_shot_clears_buffers_and_discloses_alternate_gap(self):
        pcm, source = synthesize()
        probe = fixture_probe(pcm)
        retained = (probe.reference, probe.primary, probe.alternate)
        received = np.zeros(51200)
        received[3200 : 3200 + len(source)] = source
        for index, frame in enumerate(received.reshape(-1, FRAME)):
            probe.observe(frame, frame if index else None)
        report = probe.read()
        self.assertEqual(summarize(report), report)
        self.assertEqual(report['primary']['status'], 'matched')
        self.assertEqual(report['alternate'], {'status': 'input_gap'})
        self.assertEqual(report['fresh_replay'], {'status': 'not_run'})
        self.assertEqual(
            summarize({k: v for k, v in report.items() if k != 'fresh_replay'})['status'],
            'measured',
        )
        self.assertEqual(report['alternate_frames'], len(received) - FRAME)
        self.assertEqual(probe.read(), {'status': 'unavailable'})
        self.assertTrue(all(not np.any(array) for array in retained))
        self.assertEqual(summarize({**report, 'pcm': 'secret'}), {'status': 'invalid'})

    def test_exact_matched_buffers_replay_once_and_clear_even_on_failure(self):
        pcm, source = synthesize()
        primary = np.zeros(51200)
        primary[3200 : 3200 + len(source)] = source
        alternate = -primary / 2
        for failed in (False, True):
            with self.subTest(failed=failed):
                probe = fixture_probe(pcm)
                retained = (probe.reference, probe.primary, probe.alternate)
                for left, right in zip(primary.reshape(-1, FRAME), alternate.reshape(-1, FRAME)):
                    probe.observe(left, right)

                def replay(left, right, cancelled):
                    np.testing.assert_array_equal(left, primary)
                    np.testing.assert_array_equal(right, alternate)
                    self.assertFalse(cancelled())
                    if failed:
                        raise RuntimeError('private decoder detail')
                    return {'status': 'measured', 'primary_hit': False, 'alternate_hit': True}

                with patch('local_wake.replay_english_inputs', side_effect=replay) as decode:
                    report = probe.read()
                    self.assertEqual(probe.read(), {'status': 'unavailable'})
                decode.assert_called_once()
                self.assertEqual(summarize(report), report)
                self.assertEqual(
                    report['fresh_replay']['status'], 'unavailable' if failed else 'measured'
                )
                self.assertTrue(all(not np.any(array) for array in retained))
                self.assertEqual(
                    summarize({**report, 'fresh_replay': {'status': 'measured', 'pcm': 'secret'}}),
                    {'status': 'invalid'},
                )
                if not failed:
                    self.assertEqual(
                        summarize({**report, 'alternate_frames': report['primary_frames'] - FRAME}),
                        {'status': 'invalid'},
                    )

    def test_replay_consumes_actual_frame_order_and_checks_cancellation_between_frames(self):
        primary = np.arange(3 * FRAME, dtype=float)
        alternate = -primary
        main, secondary = Mock(), Mock()
        main.alternate.return_value = secondary
        main.feed.side_effect = ('', 'Bumblebee', '')
        secondary.feed.side_effect = ('', 'BubbleBeeSpaced', '')
        with patch('local_wake.EnglishWake.load', return_value=main):
            self.assertEqual(
                replay_english_inputs(primary, alternate, lambda: False),
                {'status': 'measured', 'primary_hit': True, 'alternate_hit': False},
            )
            for decoder, source in ((main, primary), (secondary, alternate)):
                self.assertEqual(decoder.feed.call_count, 3)
                for call, frame in zip(decoder.feed.call_args_list, source.reshape(-1, FRAME)):
                    np.testing.assert_array_equal(call.args[0], frame)
                decoder.feed.reset_mock(side_effect=True)
            cancelled = Mock(side_effect=(False, False, True))
            self.assertEqual(
                replay_english_inputs(primary, alternate, cancelled), {'status': 'unavailable'}
            )
            main.feed.assert_called_once()
            secondary.feed.assert_called_once()
            cancelled_after_last = Mock(side_effect=(False, False, False, False, True))
            self.assertEqual(
                replay_english_inputs(primary, alternate, cancelled_after_last),
                {'status': 'unavailable'},
            )

    def test_expiry_and_bad_shape_zero_held_buffers(self):
        pcm, _ = synthesize()
        for finish in (
            lambda probe: probe.cancel(),
            lambda probe: probe.observe(np.zeros(17), None),
        ):
            probe = fixture_probe(pcm)
            raw = probe.reference
            probe.observe(np.ones(FRAME), None)
            finish(probe)
            self.assertEqual(probe.read(), {'status': 'unavailable'})
            self.assertFalse(np.any(raw))

    def bridge(self):
        detector = Mock(feed=Mock(return_value=''), english_input_samples=(0, 0))
        bridge = Bridge(None, None, wake_detector=detector)
        bridge.session.active = True
        bridge.session.awake = False
        bridge.session.wake = True
        return bridge, detector

    def test_only_actual_accepted_frames_enter_probe_and_capture_gap_cancels(self):
        pcm, _ = synthesize()
        bridge, detector = self.bridge()
        frame = np.zeros(FRAME)
        with (
            patch('known_speech_evidence.load_fixture', return_value=pcm),
            patch('wake_input_probe.WakeInputProbe', side_effect=fixture_probe),
        ):
            token = bridge.probes.arm_wake_input_probe(None, None, None)['token']
        probe = bridge.wake.input_probe
        bridge.wake.feed(frame, bridge.generation + 1, capture_nanos=0, alternate_pcm=frame)
        bridge.wake.feed(frame, bridge.generation, capture_nanos=0, alternate_pcm=frame)
        bridge.wake.feed(
            frame, bridge.generation, capture_nanos=1, alternate_pcm=frame, echo_risk=True
        )
        self.assertEqual(probe.frames, 2 * FRAME)
        self.assertEqual(probe.alternate_frames, FRAME)
        self.assertEqual(detector.feed.call_count, 2)
        self.assertEqual(bridge.wake.read_input_probe('f' * 32), {'status': 'unavailable'})
        bridge.wake.reset('capture_gap')
        self.assertTrue(probe.closed)
        self.assertEqual(bridge.wake.read_input_probe(token), {'status': 'unavailable'})

    def test_prearm_native_frame_is_not_copied_to_new_probe(self):
        pcm, _ = synthesize()
        bridge, detector = self.bridge()
        probe = fixture_probe(pcm)

        def during_decode(*_args, **_kwargs):
            bridge.wake.input_probe = probe
            return ''

        detector.feed.side_effect = during_decode
        bridge.wake.feed(np.zeros(FRAME), bridge.generation, capture_nanos=0)
        self.assertEqual(probe.frames, 0)
        probe.cancel()

    def test_no_arm_when_idle_and_source_reset_during_fixture_load_is_rejected(self):
        bridge, _ = self.bridge()
        bridge.session.active = False
        with patch('known_speech_evidence.load_fixture') as load:
            with self.assertRaisesRegex(RuntimeError, 'idle capture'):
                bridge.probes.arm_wake_input_probe(None, None, None)
            load.assert_not_called()
        bridge.session.active = True

        def reset(*_args):
            bridge.wake.reset('transport')
            return b''

        with patch('known_speech_evidence.load_fixture', side_effect=reset):
            with self.assertRaisesRegex(RuntimeError, 'owner changed'):
                bridge.probes.arm_wake_input_probe(None, None, None)
        self.assertIsNone(bridge.wake.input_probe)

    def test_session_change_while_analyzing_cannot_publish_old_probe(self):
        bridge, _ = self.bridge()
        pcm, _ = synthesize()
        probe = fixture_probe(pcm)
        bridge.wake.input_probe = probe
        real_read = probe.read

        def stale_read():
            with self.assertRaisesRegex(RuntimeError, 'already armed'):
                bridge.wake.arm_input_probe(pcm)
            bridge.wake.reset('transport')
            self.assertTrue(probe.cancelled.is_set())
            return real_read()

        with patch.object(probe, 'read', side_effect=stale_read):
            self.assertEqual(bridge.wake.read_input_probe(probe.token), {'status': 'unavailable'})
        self.assertTrue(probe.closed)
        self.assertIsNone(bridge.wake.input_probe)

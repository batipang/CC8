"""Exercise the real capture, owner IPC and CLI paths without a CC8 or audio."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from announce import AnnouncementServer
from device_smoke import main
from protocol import MIC, RENDER, Packet
import test_capture_gap as capture_fixture
import test_known_speech_evidence as speech_fixture
from test_known_speech_evidence import fixture


class KnownSpeechFlowTests(unittest.TestCase):
    def bridge(self):
        bridge = capture_fixture.CaptureGapTests().bridge()
        self.addCleanup(bridge.capture.close)
        return bridge

    def test_actual_ingress_keeps_aec_and_endpoint_behavior_and_matches_the_known_window(self):
        b, baseline = self.bridge(), self.bridge()
        pcm, metadata, _ = fixture()
        b.capture.arm_known_speech(pcm, metadata, b.generation, b.session.epoch)
        owner = b.capture.known_speech
        rng = np.random.default_rng(12)
        far = rng.normal(0, 0.025, 48000)
        mic = far.copy()
        near = np.frombuffer(pcm, '<i2').astype(float) / 32768
        mic[9600 : 9600 + len(near)] += near
        for sequence, offset in enumerate(range(0, len(mic), 160)):
            stamp = 1_000_000_000 + offset * 62500
            for bridge in (b, baseline):
                bridge.on_packet(
                    Packet(
                        RENDER,
                        bridge.generation,
                        sequence,
                        offset,
                        stamp,
                        (far[offset : offset + 160] * 32768).astype('<i2').tobytes(),
                    )
                )
                bridge.on_packet(
                    Packet(
                        MIC,
                        7,
                        sequence,
                        offset,
                        stamp,
                        (mic[offset : offset + 160] * 32768).astype('<i2').tobytes(),
                    )
                )
        self.assertFalse(b.capture.measuring)
        self.assertEqual(b.metrics['mic_frames'], baseline.metrics['mic_frames'])
        self.assertEqual(b.metrics['render_frames'], baseline.metrics['render_frames'])
        for key in ('token', 'generation'):
            self.assertEqual(b.playback.pause_state[key], baseline.playback.pause_state[key])
        events = capture_fixture.CaptureGapTests().events
        self.assertEqual(events(b), events(baseline))
        self.assertEqual(b.capture.endpoint.vad.talking, baseline.capture.endpoint.vad.talking)
        result = b.read_known_speech(owner.token)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['match_start_offset_frames'], 9600)
        self.assertIsNone(b.capture.known_speech)
        self.assertEqual(b.read_known_speech(owner.token), {'status': 'unavailable'})

    def test_capture_lifecycle_and_packet_gap_erase_raw_buffers(self):
        for action in ('transport', 'session', 'close', 'gap'):
            with self.subTest(action=action):
                b = self.bridge()
                pcm, metadata, _ = fixture()
                token = b.capture.arm_known_speech(pcm, metadata, b.generation, 7)
                owner = b.capture.known_speech
                owner.microphone(7, 1_000_000_000, np.ones(160))
                held = owner.mic
                if action == 'transport':
                    b.capture.reset_transport('wifi')
                elif action == 'session':
                    b.capture.reset_session()
                elif action == 'close':
                    b.capture.close()
                else:
                    b.capture.note_read_gap(None, None)
                self.assertFalse(np.any(held))
                self.assertNotEqual(b.read_known_speech(token)['status'], 'observed')

    def test_analysis_releases_session_lock_and_session_close_discards_its_result(self):
        b = self.bridge()
        pcm, metadata, _ = fixture()
        token = b.capture.arm_known_speech(pcm, metadata, b.generation, 7)
        owner = b.capture.known_speech
        entered, release = threading.Event(), threading.Event()
        analyse = owner._analyse

        def blocked(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test deadline')
            return analyse(*args)

        results = []
        with patch.object(owner, '_analyse', side_effect=blocked):
            worker = threading.Thread(target=lambda: results.append(b.read_known_speech(token)))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(b.guard.acquire(timeout=1))
                try:
                    self.assertEqual(b.read_known_speech(token)['status'], 'analysing')
                    self.assertIs(b.capture.known_speech, owner)
                    b.capture.close()
                finally:
                    b.guard.release()
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [{'status': 'unavailable'}])
        self.assertIsNone(b.capture.known_speech)

    def test_arm_requires_active_playback_and_rechecks_ownership_after_file_read(self):
        for change in ('capture', 'playing', 'generation', 'calibration', 'during_read'):
            with self.subTest(change=change):
                b = self.bridge()
                pcm, metadata, _ = fixture()
                generation = b.generation
                if change == 'capture':
                    b.session.active = False
                elif change == 'playing':
                    b.playback.playing = False
                elif change == 'generation':
                    generation -= 1
                elif change == 'calibration':
                    b.calibration.active = True

                def loaded(*_):
                    if change == 'during_read':
                        b.session.active = False
                    return pcm

                with patch('known_speech_evidence.load_fixture', side_effect=loaded) as load:
                    with self.assertRaises(RuntimeError):
                        b.arm_known_speech(
                            Path('/unused'), 'cc8-public-speech-abcdefgh', metadata, generation
                        )
                    self.assertEqual(load.call_count, int(change == 'during_read'))
                self.assertIsNone(b.capture.known_speech)

    def test_real_arm_reads_only_the_configured_private_fixture(self):
        b = self.bridge()
        with speech_fixture.FixtureFileTests().files() as (root, folder, _, _, metadata):
            result = b.arm_known_speech(root, folder.name, metadata, b.generation)
        self.assertEqual(result['status'], 'armed')
        self.assertEqual(b.capture.known_speech.token, result['token'])
        self.assertEqual(b.read_known_speech('b' * 32), {'status': 'unavailable'})
        self.assertIsNotNone(b.capture.known_speech)
        self.assertEqual(b.read_known_speech(result['token'])['status'], 'capture_incomplete')

    def test_stimulus_boundary_requires_the_live_original_playback_owner(self):
        for case in ('valid', 'token', 'epoch', 'generation', 'capture', 'playing', 'calibration'):
            with self.subTest(case=case):
                b = self.bridge()
                pcm, metadata, _ = fixture()
                token = b.capture.arm_known_speech(pcm, metadata, b.generation, 7)
                owner = b.capture.known_speech
                block = np.ones(14400) * 0.02
                owner.reference(b.generation, 0, 1_000_000_000, block)
                owner.microphone(7, 1_000_000_000, block)
                if case == 'token':
                    token = 'f' * 32
                elif case == 'epoch':
                    b.session.epoch += 1
                elif case == 'generation':
                    b.session.generation += 1
                elif case == 'capture':
                    b.session.active = False
                elif case == 'playing':
                    b.playback.playing = False
                elif case == 'calibration':
                    b.calibration.active = True
                result = b.mark_known_speech_stimulus(token)
                self.assertEqual(result['status'], 'recorded' if case == 'valid' else 'unavailable')
                self.assertEqual(
                    owner.stimulus_boundary, (14400, 14400) if case == 'valid' else None
                )

    def test_measurement_leases_are_exclusive_in_both_directions(self):
        b = self.bridge()
        pcm, metadata, _ = fixture()
        b.capture.arm_known_speech(pcm, metadata, b.generation, 7)
        with self.assertRaises(RuntimeError):
            b.capture.begin_probe(b.generation, 7)
        with self.assertRaises(RuntimeError):
            b.capture.arm_response_probe(1)
        b.capture.clear_known_speech()
        probe = b.capture.begin_probe(b.generation, 7)
        with self.assertRaises(RuntimeError):
            b.capture.arm_known_speech(pcm, metadata, b.generation, 7)
        b.capture.end_probe(probe)

    def test_analysis_error_still_zeroes_and_releases_the_owned_window(self):
        b = self.bridge()
        pcm, metadata, _ = fixture()
        token = b.capture.arm_known_speech(pcm, metadata, b.generation, 7)
        owner = b.capture.known_speech
        owner.microphone(7, 1_000_000_000, np.ones(160))
        held = owner.mic
        with patch.object(owner, '_analyse', side_effect=RuntimeError('private-body')):
            with self.assertRaises(RuntimeError):
                b.read_known_speech(token)
        self.assertFalse(np.any(held))
        self.assertIsNone(b.capture.known_speech)

    def test_owner_ipc_validates_fixed_fields_and_does_not_announce_or_start_capture(self):
        bridge = Mock()
        bridge.arm_known_speech.return_value = {'status': 'armed', 'token': 'a' * 32}
        bridge.read_known_speech.return_value = {'status': 'expired'}
        bridge.mark_known_speech_stimulus.return_value = {'status': 'insufficient_audio'}
        server = AnnouncementServer(bridge, Path('/owner/private/announce.sock'))
        arm = dict(
            operation='known_speech_arm',
            directory='cc8-public-speech-abcdefgh',
            metadata=fixture()[1],
            generation=12,
        )
        requests = (
            ({**arm, 'text': 'private'}, 'invalid_request'),
            ({**arm, 'directory': '../escape'}, 'invalid_request'),
            (arm, 'armed'),
            (
                {'operation': 'known_speech_result', 'token': 'a' * 32, 'text': 'x'},
                'invalid_request',
            ),
            ({'operation': 'known_speech_result', 'token': 'a' * 32}, 'expired'),
            ({'operation': 'known_speech_stimulus', 'token': 'a' * 32}, 'insufficient_audio'),
            (
                {'operation': 'known_speech_stimulus', 'token': 'a' * 32, 'text': 'x'},
                'invalid_request',
            ),
        )
        for payload, expected in requests:
            with self.subTest(expected=expected):
                reader, writer = socket.socketpair()
                with writer:
                    self.assertTrue(server.slots.acquire(blocking=False))
                    writer.sendall((json.dumps(payload) + '\n').encode())
                    server.handle(reader)
                    self.assertEqual(json.loads(writer.recv(4096))['status'], expected)
        bridge.arm_known_speech.assert_called_once_with(
            Path('/owner/private/public-speech-fixtures'), arm['directory'], arm['metadata'], 12
        )
        bridge.read_known_speech.assert_called_once_with('a' * 32)
        bridge.mark_known_speech_stimulus.assert_called_once_with('a' * 32)
        bridge.announce.assert_not_called()
        bridge.control_device.assert_not_called()

    def test_plan_is_explicit_and_unsupported_runtime_never_opens_device_or_files(self):
        flags = [
            '--runtime-root',
            str(Path.cwd()),
            '--capture',
            '--synthetic-barge',
            '--known-speech-evidence',
        ]
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as rpc,
            redirect_stdout(output),
        ):
            self.assertEqual(main(flags), 0)
            device.assert_not_called()
            rpc.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['minimum_backend_version'], '0.14.87')
        from release_info import RELEASE

        health = dict(
            bridge_version=RELEASE.backend_version, protocol_version=RELEASE.protocol_version
        )
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc', return_value=health),
            patch('device_smoke.private_directory') as files,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main([*flags, '--execute'])
        device.assert_not_called()
        files.assert_not_called()
        for bad in (
            ['--known-speech-room-model'],
            ['--known-speech-evidence'],
            ['--capture', '--synthetic-stop', '--known-speech-evidence'],
            ['--capture', '--interruption-suite', '--known-speech-evidence'],
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['--runtime-root', '/unused', *bad])

    def test_room_model_plan_and_capability_are_checked_before_device_access(self):
        flags = [
            '--runtime-root',
            str(Path.cwd()),
            '--capture',
            '--synthetic-barge',
            '--known-speech-evidence',
            '--known-speech-room-model',
        ]
        output = io.StringIO()
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc') as rpc,
            redirect_stdout(output),
        ):
            self.assertEqual(main(flags), 0)
            device.assert_not_called()
            rpc.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['known_speech_room_model'])
        self.assertEqual(plan['minimum_backend_version'], '0.14.88')
        from release_info import RELEASE

        health = dict(
            bridge_version=RELEASE.backend_version,
            protocol_version=RELEASE.protocol_version,
            known_speech_evidence_supported=True,
        )
        with (
            patch('device_smoke.Device') as device,
            patch('device_smoke.rpc', return_value=health),
            patch('device_smoke.private_directory') as files,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main([*flags, '--execute'])
        device.assert_not_called()
        files.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Known-source matching, isolation and cleanup without devices or real audio."""

from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

from known_speech_evidence import KnownSpeechWindow, load_fixture, MAX_FRAMES
from known_speech_receipt import validate, valid_request


def fixture():
    rng = np.random.default_rng(521)
    near = np.convolve(rng.normal(0, 0.07, 12800), [1, 0.3, -0.15], 'same')
    pcm = (near * 32768).astype('<i2').tobytes()
    target = io.BytesIO()
    with wave.open(target, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm)
    artifact = target.getvalue()
    metadata = dict(
        schema=1,
        sample_rate=16000,
        channels=1,
        sample_width_bytes=2,
        frames=len(pcm) // 2,
        pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        wav_sha256=hashlib.sha256(artifact).hexdigest(),
        acoustic_playback_confirmed=False,
    )
    return pcm, metadata, artifact


class KnownSpeechTests(unittest.TestCase):
    def probe(self):
        pcm, meta, _ = fixture()
        probe = KnownSpeechWindow(pcm, meta, 12, 7, timer=Mock())
        self.addCleanup(probe.cancel)
        return probe

    def signals(self, *, kind='mixed'):
        pcm, _, _ = fixture()
        near = np.frombuffer(pcm, '<i2').astype(float) / 32768
        rng = np.random.default_rng(87)
        far = rng.normal(0, 0.03, 48000)
        mic = far.copy()
        if kind != 'far':
            mic[9600 : 9600 + len(near)] += near
        if kind == 'repeat':
            mic[28800 : 28800 + len(near)] += near
        if kind == 'echo':
            far[:] = mic
        if kind == 'noise':
            mic[:] = rng.normal(0, 0.03, len(mic))
        if kind == 'silence':
            mic.fill(0)
        return mic, far

    def feed(self, probe, mic, far):
        for offset in range(0, len(mic), 160):
            stamp = 1_000_000_000 + offset * 62500
            probe.reference(12, offset, stamp, far[offset : offset + 160])
            probe.microphone(7, stamp, mic[offset : offset + 160])

    def test_actual_window_matches_in_mixture_and_buffers_are_zeroed_after_read(self):
        probe = self.probe()
        mic, far = self.signals()
        self.feed(probe, mic, far)
        held = (probe.mic, probe.render, probe.template)
        result = validate(probe.read())
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['match_start_offset_frames'], 9600)
        self.assertGreater(result['confidence'], 0.85)
        self.assertEqual(result['matched_window_digital_render_ms'], 200)
        self.assertFalse(result['physical_double_talk_confirmed'])
        self.assertTrue(all(not np.any(a) for a in held))
        self.assertEqual(probe.mic.size + probe.render.size + probe.template.size, 0)
        result['confidence'] = 0
        self.assertGreater(probe.read()['confidence'], 0.85)

    def test_far_only_silence_noise_ambiguity_and_echo_cannot_confirm_near_window(self):
        for kind, expected in (
            ('far', 'not_observed'),
            ('silence', 'not_observed'),
            ('noise', 'not_observed'),
            ('repeat', 'ambiguous'),
            ('echo', 'reference_not_distinct'),
        ):
            with self.subTest(kind=kind):
                probe = self.probe()
                self.feed(probe, *self.signals(kind=kind))
                result = probe.read()
                self.assertEqual(result['status'], expected)
                validate(result)
                self.assertNotIn('match_start_offset_frames', result)

    def test_gain_dc_and_polarity_do_not_change_the_matched_window(self):
        probe = self.probe()
        mic, far = self.signals()
        self.feed(probe, -0.3 * mic + 0.1, far)
        result = validate(probe.read())
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['match_start_offset_frames'], 9600)

    def test_incomplete_stale_discontinuous_and_unbounded_capture_remain_unconfirmed(self):
        for case, expected in (
            ('empty', 'capture_incomplete'),
            ('epoch', 'capture_incomplete'),
            ('mic_gap', 'mic_discontinuous'),
            ('render_gap', 'render_discontinuous'),
            ('nan', 'invalid_audio'),
            ('limit', 'mic_limit_exceeded'),
        ):
            with self.subTest(case=case):
                probe = self.probe()
                block = np.ones(160) * 0.01
                held = probe.mic
                if case == 'epoch':
                    probe.microphone(6, 1_000_000_000, block)
                    probe.reference(11, 0, 1_000_000_000, block)
                elif case == 'mic_gap':
                    probe.microphone(7, 1_000_000_000, block)
                    probe.microphone(7, 1_030_000_000, block)
                elif case == 'render_gap':
                    probe.reference(12, 0, 1_000_000_000, block)
                    probe.reference(12, 320, 1_020_000_000, block)
                elif case == 'nan':
                    probe.microphone(7, 1_000_000_000, np.array([float('nan')]))
                elif case == 'limit':
                    probe.microphone(7, 1_000_000_000, np.ones(MAX_FRAMES + 1))
                result = validate(probe.read())
                self.assertEqual(result['status'], expected)
                self.assertFalse(np.any(held))

    def test_deadline_and_late_cancel_clear_data_and_reject_old_observed_result(self):
        pcm, meta, _ = fixture()
        timer = Mock()
        probe = KnownSpeechWindow(pcm, meta, 12, 7, timer=timer)
        self.feed(probe, *self.signals())
        held = probe.mic
        timer.assert_called_once()
        timer.call_args.args[1]()
        self.assertEqual(validate(probe.read())['status'], 'expired')
        self.assertFalse(np.any(held))
        second = self.probe()
        self.feed(second, *self.signals())
        self.assertEqual(second.read()['status'], 'observed')
        second.cancel('capture_closed')
        self.assertEqual(validate(second.read())['status'], 'capture_closed')

    def test_cancellation_during_analysis_cannot_publish_a_stale_match(self):
        probe = self.probe()
        self.feed(probe, *self.signals())
        held = (probe.mic, probe.render, probe.template)
        entered, release = threading.Event(), threading.Event()
        analyse = probe._analyse

        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return analyse(*args)

        results = []
        with patch.object(probe, '_analyse', side_effect=blocked):
            worker = threading.Thread(target=lambda: results.append(probe.read()))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(probe.read()['status'], 'analysing')
                probe.cancel('transport_changed')
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(validate(results[0])['status'], 'transport_changed')
        self.assertTrue(all(not np.any(a) for a in held))

    def test_receipts_and_requests_reject_content_false_claims_and_invalid_numbers(self):
        probe = self.probe()
        self.feed(probe, *self.signals())
        result = validate(probe.read())
        for key, value in (
            ('text', 'private'),
            ('raw_audio_saved', True),
            ('physical_double_talk_confirmed', True),
            ('confidence', float('nan')),
            ('render_similarity', 0.6),
            ('match_start_offset_frames', 96000),
            ('match_start_offset_frames', 1),
            ('matched_window_digital_render_ms', 201),
            ('captured_frames', True),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({**result, key: value})
        with self.assertRaises(ValueError):
            validate({**result, 'captured_frames': 3200, 'match_start_offset_frames': 0})
        request = dict(
            operation='known_speech_arm',
            generation=12,
            directory='cc8-public-speech-abcdefgh',
            metadata=fixture()[1],
        )
        self.assertTrue(valid_request(request))
        for changed in (
            {'directory': '../other'},
            {'generation': True},
            {'text': 'private'},
            {'metadata': {}},
        ):
            self.assertFalse(valid_request({**request, **changed}))
        self.assertTrue(valid_request({'operation': 'known_speech_result', 'token': 'a' * 32}))
        self.assertFalse(
            valid_request({'operation': 'known_speech_result', 'token': 'a' * 32, 'text': 'x'})
        )


class FixtureFileTests(unittest.TestCase):
    @contextmanager
    def files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            folder = root / 'cc8-public-speech-abcdefgh'
            folder.mkdir(mode=0o700)
            pcm, metadata, artifact = fixture()
            path = folder / 'source.wav'
            path.write_bytes(artifact)
            path.chmod(0o600)
            yield root, folder, path, pcm, metadata

    def test_reads_same_snapshot_without_returning_paths_or_content(self):
        with self.files() as (root, folder, path, pcm, metadata):
            self.assertEqual(load_fixture(root, folder.name, metadata), pcm)
            self.assertTrue(path.exists())

    def test_matching_file_digest_does_not_accept_malformed_wave_headers(self):
        for artifact in (b'', b'not a WAV', b'RIFF\x04\0\0\0WAVE'):
            with (
                self.subTest(size=len(artifact)),
                self.files() as (root, folder, path, _, metadata),
            ):
                path.write_bytes(artifact)
                metadata['wav_sha256'] = hashlib.sha256(artifact).hexdigest()
                with self.assertRaises(ValueError):
                    load_fixture(root, folder.name, metadata)

    def test_file_changes_permissions_links_and_foreign_paths_fail_closed(self):
        for case in (
            'changed',
            'digest',
            'group',
            'parent',
            'root',
            'symlink',
            'directory_link',
            'hardlink',
            'oversized',
            'escape',
        ):
            with self.subTest(case=case), self.files() as (root, folder, path, _, metadata):
                directory = folder.name
                if case == 'changed':
                    path.write_bytes(b'wrong')
                elif case == 'digest':
                    metadata['pcm_sha256'] = '0' * 64
                elif case == 'group':
                    path.chmod(0o640)
                elif case == 'parent':
                    folder.chmod(0o755)
                elif case == 'root':
                    root.chmod(0o755)
                elif case == 'symlink':
                    alternate = folder / 'other.wav'
                    path.rename(alternate)
                    path.symlink_to(alternate)
                elif case == 'directory_link':
                    alternate = root / 'other'
                    folder.rename(alternate)
                    folder.symlink_to(alternate)
                elif case == 'hardlink':
                    import os

                    os.link(path, folder / 'linked.wav')
                elif case == 'oversized':
                    with path.open('r+b') as output:
                        output.truncate(1024 * 1024 + 1)
                elif case == 'escape':
                    directory = '../cc8-public-speech-abcdefgh'
                with self.assertRaises((OSError, ValueError)):
                    load_fixture(root, directory, metadata)


if __name__ == '__main__':
    unittest.main()

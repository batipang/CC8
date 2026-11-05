"""Independent keyword streams and serialized native reset; no real model/capture."""

from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np
from fast_stop import FastStopDetector
from local_wake import WakeDetector, ChineseWake, EnglishWake


class Stream:
    def __init__(self):
        self.results = []
        self.last = ''
        self.audio = []

    def accept_waveform(self, sample_rate, samples):
        assert sample_rate == 16000
        self.audio.append(samples.copy())


class Backend:
    def __init__(self):
        self.streams = []
        self.reset_count = 0

    def create_stream(self, keywords=None):
        stream = Stream()
        self.streams.append((stream, keywords))
        return stream

    def is_ready(self, stream):
        return bool(stream.results)

    def decode_stream(self, stream):
        stream.last = stream.results.pop(0)

    def get_result(self, stream):
        return stream.last

    def reset_stream(self, stream):
        self.reset_count += 1
        stream.last = ''


class LocalWakeTests(unittest.TestCase):
    def test_only_english_wake_uses_wider_decoder_search(self):
        model_files = (
            'tokens.txt',
            'encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
            'decoder-epoch-12-avg-2-chunk-16-left-64.onnx',
            'joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
            'encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx',
            'decoder-epoch-13-avg-2-chunk-8-left-64.onnx',
            'joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx',
        )
        english, chinese = Backend(), Backend()
        spotter = Mock(side_effect=(chinese, english))
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory)
            for name in model_files:
                (model / name).touch()
            with patch.dict(
                'sys.modules', {'sherpa_onnx': SimpleNamespace(KeywordSpotter=spotter)}
            ):
                stop = FastStopDetector(model=model)
                wake = WakeDetector.load(stop, english_model=model)

        stop_call, english_call = spotter.call_args_list
        self.assertNotIn('max_active_paths', stop_call.kwargs)
        self.assertEqual(english_call.kwargs.get('max_active_paths'), 8)
        self.assertEqual(english_call.kwargs['keywords_threshold'], 0.25)
        self.assertIs(wake.chinese.detector, stop.detector)
        self.assertIs(wake.english.detector, english)

    def pair(self):
        backend = Backend()
        with patch('fast_stop.keyword_spotter', return_value=backend):
            stop = FastStopDetector()
        wake = WakeDetector(
            ChineseWake(stop.detector, lock=stop.engine_lock), EnglishWake(Backend())
        )
        return backend, stop, wake

    def test_chinese_shares_stop_weights_and_english_is_an_independent_keyword_stream(self):
        backend, stop, wake = self.pair()
        self.assertIs(stop.detector, wake.chinese.detector)
        self.assertIs(stop.engine_lock, wake.chinese.engine_lock)
        self.assertIsNot(stop.stream, wake.chinese.stream)
        self.assertIsNone(backend.streams[0][1])
        self.assertEqual(backend.streams[1][1], 'd à h uáng f ēng @大黄蜂')
        stop.stream.results = ['大黄蜂', '暂停']
        wake.chinese.stream.results = ['停止', 'unknown', '大黄蜂']
        wake.english.stream.results = ['停止']
        self.assertEqual(stop.feed(np.zeros(320)), '暂停')
        self.assertEqual(wake.feed(np.zeros(320)), '大黄蜂')
        wake.english.stream.results = ['Bumblebee']
        self.assertEqual(wake.feed(np.zeros(320)), 'Bumblebee')
        self.assertEqual(backend.reset_count, 5)

    def test_wake_reset_cannot_replace_stop_stream_and_can_preserve_counters(self):
        backend, stop, wake = self.pair()
        old_stop, old_wake = stop.stream, wake.chinese.stream
        wake.feed(np.zeros(320))
        wake.reset(clear_metrics=False)
        self.assertIs(stop.stream, old_stop)
        self.assertIsNot(wake.chinese.stream, old_wake)
        self.assertEqual(wake.samples, 320)
        wake.reset()
        self.assertEqual(wake.samples, 0)
        self.assertEqual(wake.compute_seconds, 0)

    def test_invalid_frames_fail_before_any_native_accept(self):
        _, _, wake = self.pair()
        for pcm in (np.zeros((2, 160)), np.zeros(6401), np.array([np.nan]), np.array([np.inf])):
            with self.subTest(shape=pcm.shape), self.assertRaises(ValueError):
                wake.feed(pcm)
        self.assertEqual(wake.chinese.stream.audio, [])
        self.assertEqual(wake.samples, 0)

    def test_gain_is_bounded_and_clipped_without_changing_stop_pcm(self):
        backend, stop, wake = self.pair()
        for gain in (0, 5, True, float('nan')):
            with self.subTest(gain_type=type(gain).__name__), self.assertRaises(ValueError):
                ChineseWake(backend, gain)
        frame = np.array([0.01, -0.01, 1, -1])
        stop.feed(frame)
        wake.feed(frame)
        np.testing.assert_allclose(stop.stream.audio[0], [0.02, -0.02, 0.98, -0.98])
        np.testing.assert_array_equal(stop.stream.audio[0], wake.chinese.stream.audio[0])

    def test_reset_of_wake_stream_waits_for_other_stream_native_decode(self):
        backend, stop, wake = self.pair()
        entered, release, reset_started, created = [threading.Event() for _ in range(4)]
        errors, results = [], []
        original_decode, original_create = backend.decode_stream, backend.create_stream

        def decode(stream):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test decoder release')
            original_decode(stream)

        def create(keywords=None):
            created.set()
            return original_create(keywords)

        def feed():
            try:
                results.append(stop.feed(np.zeros(320)))
            except Exception as error:
                errors.append(error)

        def reset():
            reset_started.set()
            try:
                wake.reset()
            except Exception as error:
                errors.append(error)

        backend.decode_stream, backend.create_stream = decode, create
        stop.stream.results = ['停一下']
        feeder = threading.Thread(target=feed, daemon=True)
        resetter = threading.Thread(target=reset, daemon=True)
        feeder.start()
        try:
            self.assertTrue(entered.wait(1))
            resetter.start()
            self.assertTrue(reset_started.wait(1))
            self.assertFalse(created.wait(0.03), 'shared model accessed concurrently')
        finally:
            release.set()
            feeder.join(2)
            if resetter.ident is not None:
                resetter.join(2)
        self.assertFalse(feeder.is_alive())
        self.assertFalse(resetter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, ['停一下'])
        self.assertTrue(created.is_set())


if __name__ == '__main__':
    unittest.main()

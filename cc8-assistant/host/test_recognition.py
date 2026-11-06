import types
import unittest
import numpy as np

from recognition import MandarinRecognizer, prepare_audio
from dsp import FRAME, MAX_UTTERANCE_FRAMES, VoiceDetector, ReferenceCapture


class FakeRecognizer:
    def __init__(self, text):
        self.text = text
        self.decoded = 0
        self.audio = None

    def create_stream(self):
        owner = self

        class Stream:
            result = types.SimpleNamespace(text=owner.text)

            def accept_waveform(self, rate, pcm):
                owner.rate = rate
                owner.audio = pcm

        return Stream()

    def decode_stream(self, stream):
        self.decoded += 1


class RecognitionTests(unittest.TestCase):
    def test_silence_and_dc_offset_never_reach_decoder(self):
        backend = FakeRecognizer('不能出现')
        asr = MandarinRecognizer(recognizer=backend)
        for pcm in [np.zeros(16000), np.ones(16000) * 0.2, np.full(16000, np.nan)]:
            self.assertEqual(asr.recognize(pcm), '')
        self.assertEqual(backend.decoded, 0)

    def test_quiet_audio_is_bounded_and_padded_without_changing_sample_rate(self):
        wave = np.sin(np.arange(16000) * 0.3) * 0.008
        pcm, gain = prepare_audio(wave)
        self.assertGreater(gain, 1)
        self.assertLessEqual(gain, 6)
        self.assertEqual(len(pcm), 16000 + 4480)
        self.assertLessEqual(float(np.max(np.abs(pcm))), 0.98)
        backend = FakeRecognizer('今天天气怎么样')
        asr = MandarinRecognizer(recognizer=backend)
        self.assertEqual(asr.recognize(wave), '今天天气怎么样')
        self.assertEqual(backend.rate, 16000)

    def test_loud_audio_is_not_clipped_by_gain(self):
        pcm, gain = prepare_audio(np.sin(np.arange(16000) * 0.4))
        self.assertLess(gain, 1)
        self.assertLessEqual(float(np.max(np.abs(pcm))), 0.921)

    def test_known_name_only_is_normalized_and_tags_are_removed(self):
        backend = FakeRecognizer('<|zh|><|NEUTRAL|>帮不币，告诉我常州的天气')
        text = MandarinRecognizer(recognizer=backend).recognize(
            np.sin(np.arange(16000) * 0.1) * 0.03
        )
        self.assertEqual(text, 'Bumblebee，告诉我常州的天气')

    def test_noisy_or_unsupported_input_bounds(self):
        with self.assertRaises(ValueError):
            prepare_audio(np.ones((100, 2)))
        with self.assertRaises(ValueError):
            prepare_audio(np.ones(16000 * 14))

    def test_sentence_pause_does_not_cut_a_slowly_spoken_question(self):
        vad = VoiceDetector()
        ends = []
        frames = (
            [(True, np.ones(FRAME) * 0.02)] * 30
            + [(False, np.zeros(FRAME))] * 65
            + [(True, np.ones(FRAME) * 0.02)] * 40
            + [(False, np.zeros(FRAME))] * 85
        )
        for voiced, pcm in frames:
            vad.external_voice = voiced
            _, utterance, _ = vad.feed(pcm)
            if utterance is not None:
                ends.append(utterance)
        self.assertEqual(len(ends), 1)

    def test_sentence_at_audio_cap_can_finish_in_trailing_silence(self):
        vad = VoiceDetector()
        reference = ReferenceCapture()
        near, render = np.ones(FRAME), np.full(FRAME, 0.5)
        quiet = np.zeros(FRAME)
        outputs = []
        for index in range(MAX_UTTERANCE_FRAMES + vad.END_SILENCE_FRAMES):
            voiced = index < MAX_UTTERANCE_FRAMES
            vad.external_voice = voiced
            began, audio, _ = vad.feed(near if voiced else quiet)
            aligned = reference.feed(render if voiced else quiet, began, audio is not None)
            if audio is not None:
                outputs.append((index, audio, aligned))
        self.assertEqual(len(outputs), 1)
        index, audio, aligned = outputs[0]
        self.assertEqual(index, MAX_UTTERANCE_FRAMES + vad.END_SILENCE_FRAMES - 1)
        self.assertEqual(len(audio), MAX_UTTERANCE_FRAMES * FRAME)
        np.testing.assert_array_equal(audio, aligned * 2)

    def test_longer_preroll_keeps_soft_syllables_and_matching_reference(self):
        vad = VoiceDetector()
        capture = ReferenceCapture()
        result = None
        for n in range(130):
            voiced = 40 <= n < 55
            pcm = np.full(FRAME, 0.005 if n < 40 else 0.03 if voiced else 0)
            vad.external_voice = voiced
            began, done, _ = vad.feed(pcm)
            ref = capture.feed(np.full(FRAME, n), began, done is not None)
            if done is not None:
                result = (done, ref)
        # Finish the 800 ms endpoint.
        for n in range(130, 145):
            vad.external_voice = False
            began, done, _ = vad.feed(np.zeros(FRAME))
            ref = capture.feed(np.full(FRAME, n), began, done is not None)
            if done is not None:
                result = (done, ref)
        self.assertIsNotNone(result)
        audio, ref = result
        self.assertEqual(len(audio), len(ref))
        self.assertGreaterEqual(np.count_nonzero(audio == 0.005), FRAME * 30)


if __name__ == '__main__':
    unittest.main()

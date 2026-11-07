"""Signal and malformed-input boundaries for same-grid candidate references."""

import unittest
import numpy as np
from paired_capture import decode_pairs, PairedReferenceFilter, REFERENCE_GAIN


class PairedReferenceTests(unittest.TestCase):
    def test_decode_distinct_interleaved_channels_and_own_output(self):
        source = np.array([[1000, -2000], [-3000, 4000]], dtype='<i2').tobytes()
        mic, ref = decode_pairs(source)
        np.testing.assert_array_equal(mic * 32768, [1000, -3000])
        np.testing.assert_array_equal(ref * 32768, [-2000, 4000])
        mic.fill(0)
        np.testing.assert_array_equal(ref * 32768, [-2000, 4000])
        self.assertNotEqual(source, bytes(len(source)))

    def test_partial_pair_empty_and_oversize_rejected(self):
        for value in (b'', b'1', b'12', b'123', bytes(6404), bytearray(4), None):
            with self.subTest(size=len(value) if value is not None else None):
                with self.assertRaises(ValueError):
                    decode_pairs(value)
        self.assertEqual(len(decode_pairs(bytes(6400))[0]), 1600)

    def test_equal_inputs_keep_identical_filter_phase(self):
        pair = PairedReferenceFilter()
        rng = np.random.default_rng(3)
        for _ in range(120):
            source = rng.normal(0, 0.1, 160)
            before = source.copy()
            mic, ref = pair.process(source, source)
            np.testing.assert_array_equal(mic, ref / REFERENCE_GAIN)
            np.testing.assert_array_equal(source, before)

    def test_constant_offset_is_removed_without_muting_voice_band(self):
        pair = PairedReferenceFilter()
        time = np.arange(16000) / 16000
        signal = 0.1 + 0.1 * np.sin(2 * np.pi * 1000 * time)
        filtered = np.concatenate(
            [pair.process(chunk, chunk)[0] for chunk in signal.reshape(-1, 160)]
        )
        self.assertLess(abs(np.mean(filtered[8000:])), 0.0001)
        self.assertAlmostEqual(
            float(np.sqrt(np.mean(filtered[8000:] ** 2))), 0.1 / np.sqrt(2), delta=0.001
        )

    def test_reset_prevents_old_dc_or_reference_entering_new_epoch(self):
        pair = PairedReferenceFilter()
        for _ in range(4):
            pair.process(np.ones(160), -np.ones(160))
        pair.clear()
        mic, ref = pair.process(np.zeros(160), np.zeros(160))
        self.assertFalse(np.any(mic))
        self.assertFalse(np.any(ref))

    def test_invalid_second_stream_does_not_advance_first(self):
        pair = PairedReferenceFilter()
        signal = np.linspace(-0.2, 0.2, 160)
        for bad in (
            np.zeros(159),
            np.full(160, np.nan),
            np.zeros((160, 1)),
            np.full(160, np.inf),
            np.ones(160, dtype=complex),
            np.full(160, 2.0),
            np.full(160, 'text'),
        ):
            with self.subTest(shape=bad.shape):
                with self.assertRaises(ValueError):
                    pair.process(signal, bad)
        clean = PairedReferenceFilter()
        actual = pair.process(signal, signal)
        expected = clean.process(signal, signal)
        for a, e in zip(actual, expected):
            np.testing.assert_array_equal(a, e)

    def test_only_supplied_prefix_changes_future_outputs(self):
        pair = PairedReferenceFilter()
        copy = PairedReferenceFilter()
        prefix = np.linspace(-0.4, 0.4, 160)
        first = pair.process(prefix, prefix)
        same = copy.process(prefix, prefix)
        for a, b in zip(first, same):
            np.testing.assert_array_equal(a, b)
        pair.process(np.ones(160), np.zeros(160))
        # The returned prefix is owned independently of future filter history.
        for a, b in zip(first, same):
            np.testing.assert_array_equal(a, b)


if __name__ == '__main__':
    unittest.main()

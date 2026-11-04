"""Identical audio with opposite labels is invalid input, not a model result."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import check_wake

CASES = (
    ('positive', 'Fixed voice', 170, 'Bumblebee', True),
    ('negative', 'Fixed voice', 170, 'Bumbleby', False),
)
PCM = b'\x40\x00' * 1600
OTHER = b'\xc0\x00' * 1600


class FixtureIntegrityTests(unittest.TestCase):
    @contextmanager
    def environment(self, pcm, cases=CASES):
        from local_wake import EnglishWake

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / 'fixed-native-placeholder.so'
            binary.write_bytes(b'fixed test provenance')
            lib = ModuleType('sherpa_onnx.lib')
            lib._sherpa_onnx = SimpleNamespace(__file__=str(binary))
            parent = ModuleType('sherpa_onnx')
            parent.lib = lib
            feeds = Mock(return_value=False)
            detector = SimpleNamespace(
                detector=object(),
                gain=1,
                engine_lock=threading.Lock(),
                reset=lambda: None,
                feed=feeds,
            )

            class Cold:
                def __init__(self, *args, **kwargs):
                    self.feed = feeds

                def reset(self):
                    pass

            reports = []
            output = SimpleNamespace(write=lambda value: reports.append(deepcopy(value)))
            with (
                patch.dict(sys.modules, {'sherpa_onnx': parent, 'sherpa_onnx.lib': lib}),
                patch.object(check_wake, 'CASES', cases),
                patch.object(check_wake, 'resolve_voice', side_effect=lambda voice: voice),
                patch.object(
                    check_wake,
                    'synthesize',
                    side_effect=lambda folder, voice, rate, text: pcm[text],
                ) as synth,
                patch('tools.cc8.verified_models', return_value=True),
                patch.object(EnglishWake, 'load', return_value=detector) as load,
                patch('streaming_keywords.StreamingKeywords', Cold),
            ):
                yield SimpleNamespace(
                    root=root, output=output, reports=reports, feeds=feeds, load=load, synth=synth
                )

    def test_conflicting_pcm_is_rejected_before_any_native_scoring(self):
        with self.environment({'Bumblebee': PCM, 'Bumbleby': PCM}) as env:
            state, counts = check_wake.evaluate(env.root, env.output)
            self.assertEqual(state, 'input_invalid')
            env.load.assert_not_called()
            env.feeds.assert_not_called()
            final = env.reports[-1]
            conflicts = final.get('input_integrity', {}).get('conflicts', [])
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(set(conflicts[0]['cases']), {'positive', 'negative'})
            self.assertEqual([r['expected_keyword'] for r in final['samples']], [True, False])
            self.assertFalse(any(r.get('state') == 'measured' for r in final['samples']))
            self.assertTrue(
                all(v['positive_samples'] == v['negative_samples'] == 0 for v in counts.values())
            )

    def test_equal_labels_may_share_pcm_without_fabricating_independent_voices(self):
        cases = (*CASES, ('same-positive', 'Fixed voice', 140, 'same target', True))
        with self.environment(
            {'Bumblebee': PCM, 'Bumbleby': OTHER, 'same target': PCM}, cases
        ) as env:
            state, _ = check_wake.evaluate(env.root, env.output)
            self.assertEqual(state, 'failed')  # Fixed fake model misses positives.
            env.load.assert_called_once()
            self.assertGreater(env.feeds.call_count, 0)
            integrity = env.reports[-1].get('input_integrity', {})
            self.assertEqual(integrity.get('state'), 'verified_exact_pcm_labels')
            self.assertEqual(integrity.get('same_label_duplicate_groups'), 1)
            self.assertFalse(integrity.get('phonetic_correctness_asserted', True))

    def test_valid_inputs_preserve_scores_and_the_success_path(self):
        with self.environment({'Bumblebee': PCM, 'Bumbleby': OTHER}) as env:
            env.feeds.side_effect = lambda frame: any(0 < value < 0.003 for value in frame)
            state, counts = check_wake.evaluate(env.root, env.output)
            self.assertEqual(state, 'passed')
            env.load.assert_called_once()
            for value in counts.values():
                self.assertEqual(
                    value,
                    {
                        'positive_samples': 1,
                        'positive_hits': 1,
                        'negative_samples': 1,
                        'false_hits': 0,
                    },
                )
            self.assertTrue(check_wake.passed(env.reports[-1]['samples']))

    def test_pass_gate_requires_valid_digest_and_consistent_labels(self):
        rows = [
            dict(
                case=c[0],
                expected_keyword=c[4],
                state='measured',
                fixture_pcm_sha256=hashlib.sha256(c[0].encode()).hexdigest(),
                **{k: c[4] for k in check_wake.CONDITIONS},
            )
            for c in check_wake.CASES
        ]
        self.assertTrue(check_wake.passed(rows))
        for kind in ('missing', 'invalid', 'conflicting', 'bad_label_type'):
            bad = deepcopy(rows)
            if kind == 'missing':
                bad[0].pop('fixture_pcm_sha256')
            elif kind == 'invalid':
                bad[0]['fixture_pcm_sha256'] = 'not-a-digest'
            elif kind == 'conflicting':
                negative = next(r for r in bad if not r['expected_keyword'])
                negative['fixture_pcm_sha256'] = bad[0]['fixture_pcm_sha256']
            else:
                bad[0]['expected_keyword'] = []
            with self.subTest(kind=kind):
                self.assertFalse(check_wake.passed(bad))

    def test_bad_pcm_prevents_the_whole_matrix_from_being_scored(self):
        for pcm in (b'', b'x', b'\0' * 320002):
            with (
                self.subTest(size=len(pcm)),
                self.environment({'Bumblebee': pcm, 'Bumbleby': OTHER}) as env,
            ):
                state, _ = check_wake.evaluate(env.root, env.output)
                self.assertEqual(state, 'input_invalid')
                env.load.assert_not_called()
                env.feeds.assert_not_called()

    def test_fixture_error_does_not_score_the_remaining_cases(self):
        with self.environment({'Bumblebee': PCM, 'Bumbleby': OTHER}) as env:
            env.synth.side_effect = [ValueError('fixed generation error'), OTHER]
            state, _ = check_wake.evaluate(env.root, env.output)
            self.assertEqual(state, 'input_invalid')
            env.load.assert_not_called()
            env.feeds.assert_not_called()
            self.assertEqual(env.reports[-1]['samples'][0]['state'], 'fixture_failed')

    def test_cancel_during_preparation_never_loads_model_and_preserves_cancelled(self):
        with self.environment({'Bumblebee': PCM, 'Bumbleby': OTHER}) as env:
            env.synth.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                check_wake.evaluate(env.root, env.output)
            env.load.assert_not_called()
            self.assertEqual(env.reports[-1]['state'], 'cancelled')

    def test_conflict_does_not_require_optional_numpy_or_native_imports(self):
        with self.environment({'Bumblebee': PCM, 'Bumbleby': PCM}) as env:
            with patch.dict(sys.modules, {'numpy': None, 'sherpa_onnx.lib': None}):
                try:
                    state, _ = check_wake.evaluate(env.root, env.output)
                except ImportError:
                    self.fail('Invalid fixtures reached optional model imports')
            self.assertEqual(state, 'input_invalid')
            env.load.assert_not_called()

    def test_model_preflight_failure_finishes_prepared_report_without_scoring(self):
        with self.environment({'Bumblebee': PCM, 'Bumbleby': OTHER}) as env:
            with patch('tools.cc8.verified_models', return_value=False):
                with self.assertRaises(ValueError):
                    check_wake.evaluate(env.root, env.output)
            self.assertTrue(env.reports)
            self.assertEqual(env.reports[-1]['state'], 'model_unavailable')
            env.load.assert_not_called()
            env.feeds.assert_not_called()


if __name__ == '__main__':
    unittest.main()

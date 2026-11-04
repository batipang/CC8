"""Public synthetic checks never silently treat missing/failed inputs as success."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch
from types import SimpleNamespace
from tools.check_wake import (
    CASES,
    CONDITIONS,
    available_voices,
    parse_voices,
    passed,
    read_pcm,
    resolve_voice,
    summary,
    synthesize,
)


class SyntheticWakeTests(unittest.TestCase):
    def rows(self):
        return [
            dict(
                case=c[0],
                expected_keyword=c[4],
                state='measured',
                fixture_pcm_sha256=hashlib.sha256(c[0].encode()).hexdigest(),
                **{k: c[4] for k in CONDITIONS},
            )
            for c in CASES
        ]

    def test_real_minimal_path_plan_never_loads_models_or_creates_directories(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'absent'
            result = subprocess.run(
                [sys.executable, str(root / 'cc8'), 'verify-wake', '--model-root', str(path)],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertFalse(plan['execute'])
            self.assertFalse(plan['microphone_used'])
            self.assertFalse(plan['network_requested'])
            self.assertEqual(plan['sample_count'], len(CASES))
            self.assertFalse(path.exists())

    def test_fixed_corpus_has_distinct_cases_and_real_near_name_negatives(self):
        self.assertEqual(len({c[0] for c in CASES}), len(CASES))
        self.assertEqual(sum(c[4] for c in CASES), 15)
        self.assertEqual(sum(not c[4] for c in CASES), 10)
        self.assertIn('Bubble bee', {c[3] for c in CASES if not c[4]})

    def test_missing_failed_duplicate_and_wrong_answers_cannot_pass(self):
        self.assertTrue(passed(self.rows()))
        for kind in ('missing', 'failed', 'duplicate', 'wrong', 'wrong_label', 'missing_condition'):
            rows = self.rows()
            if kind == 'missing':
                rows.pop()
            elif kind == 'failed':
                rows[0]['state'] = 'fixture_or_model_failed'
            elif kind == 'duplicate':
                rows[-1] = dict(rows[0])
            elif kind == 'wrong':
                rows[0]['production_immediate'] = False
            elif kind == 'wrong_label':
                rows[0]['expected_keyword'] = False
                for key in CONDITIONS:
                    rows[0][key] = False
            else:
                rows[0].pop('production_continuous')
            with self.subTest(kind=kind):
                self.assertFalse(passed(rows))

    def test_cold_reference_misses_do_not_obscure_warm_or_negative_results(self):
        rows = self.rows()
        rows[0]['cold_reference'] = False
        self.assertTrue(passed(rows))
        counts = summary(rows)
        self.assertEqual(counts['cold_reference']['positive_hits'], 14)
        self.assertEqual(counts['production_immediate']['positive_hits'], 15)
        next(r for r in rows if not r['expected_keyword'])['production_immediate'] = True
        self.assertFalse(passed(rows))
        self.assertEqual(summary(rows)['production_immediate']['false_hits'], 1)

    def test_localized_voice_inventory_deduplicates_and_keeps_actual_names(self):
        text = (
            'Samantha (英语（美国）)   en_US # Hello\n'
            'Samantha (英语（美国）)   en_US # Hello\n'
            'Karen (英语（澳大利亚）) en_AU # Hello\n'
        )
        names = parse_voices(text)
        self.assertEqual(len(names), 2)
        self.assertIn('Samantha (英语（美国）)', names)
        for invalid in ('', 'unparseable inventory', 'x' * 128001):
            with self.assertRaises(ValueError):
                parse_voices(invalid)

    def test_unknown_voice_is_rejected_before_silent_say_fallback(self):
        available_voices.cache_clear()
        try:
            # macOS can return zero and synthesize the default for an unknown
            # name. Only the inventory query may run in this case.
            with (
                tempfile.TemporaryDirectory() as folder,
                patch(
                    'tools.check_wake.subprocess.run',
                    return_value=SimpleNamespace(stdout='Samantha en_US # Hello\n', returncode=0),
                ) as run,
            ):
                with self.assertRaises(ValueError):
                    synthesize(Path(folder), 'Alex', 170, 'Bumblebee')
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], ['/usr/bin/say', '-v', '?'])
                self.assertFalse(any(Path(folder).iterdir()))
        finally:
            available_voices.cache_clear()

    def test_ambiguous_named_voice_is_not_implicitly_chosen(self):
        with patch(
            'mac_voice.available_voices',
            return_value=frozenset(
                (
                    'Samantha (English US)',
                    'Samantha (English GB)',
                )
            ),
        ):
            with self.assertRaises(ValueError):
                resolve_voice('Samantha')
            self.assertEqual(resolve_voice('Samantha (English US)'), 'Samantha (English US)')

    def test_named_synthesis_uses_resolved_name_and_default_is_explicit(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                'mac_voice.available_voices',
                return_value=frozenset(('Samantha (English US)',)),
            ) as voices,
            patch('tools.check_wake.subprocess.run') as run,
            patch('mac_voice.read_pcm', return_value=b'\0' * 320),
        ):
            synthesize(Path(folder), 'Samantha', 170, 'Bumblebee')
            self.assertEqual(run.call_args_list[0].args[0][1:3], ['-v', 'Samantha (English US)'])
            voices.reset_mock()
            run.reset_mock()
            synthesize(Path(folder), None, 170, 'Bumblebee')
            voices.assert_not_called()
            self.assertNotIn('-v', run.call_args_list[0].args[0])

    def test_corpus_retains_old_fallback_as_three_explicit_default_cases(self):
        default = [case for case in CASES if case[1] is None]
        self.assertEqual(
            {case[0] for case in default},
            {'legacy-default-140', 'legacy-default-170', 'legacy-default-200'},
        )
        self.assertTrue(all(case[4] for case in default))
        self.assertIn('Karen', {case[1] for case in CASES})
        self.assertNotIn('Alex', {case[1] for case in CASES})

    def test_interrupt_returns_cancelled_exit_without_success(self):
        from tools.check_wake import main

        with (
            tempfile.TemporaryDirectory() as folder,
            patch('tools.check_wake.ROOT', Path(folder).resolve()),
            patch('tools.check_wake.platform.system', return_value='Darwin'),
            patch('tools.check_wake.evaluate', side_effect=KeyboardInterrupt),
        ):
            (Path(folder) / 'device-access').mkdir()
            self.assertEqual(main(['--model-root', str(Path(folder).resolve()), '--execute']), 130)

    def test_fixture_pcm_is_bounded_and_rejects_empty_or_wrong_format(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.wav'
            for channels, frames in ((1, 0), (2, 160), (1, 160001)):
                with wave.open(str(path), 'wb') as out:
                    out.setnchannels(channels)
                    out.setsampwidth(2)
                    out.setframerate(16000)
                    out.writeframes(b'\0' * (frames * channels * 2))
                with self.assertRaises(ValueError):
                    read_pcm(path)
            with wave.open(str(path), 'wb') as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(b'\0' * 320)
            self.assertEqual(len(read_pcm(path)), 320)
            data = path.read_bytes()
            path.write_bytes(data[:-2])
            with self.assertRaises(ValueError):
                read_pcm(path)


if __name__ == '__main__':
    unittest.main()

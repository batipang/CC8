"""Scoring mathematics, missing coverage, paired A/B and private CLI boundaries."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from asr_evaluation import CORPUS, corpus_samples, edit_counts, evaluate, normalized, read_json
from score_asr import ROOT

SECRET = 'PRIVATE-utterance-location-not-for-report'


def corpus():
    return {
        'schema': 1,
        'corpus': 'cc8-generic-v1',
        'samples': [
            {
                'id': 'qa-01',
                'category': 'qa',
                'partition': 'acceptance',
                'delivery': 'normal',
                'reference': '苏州下午三点',
                'critical_fields': {'place': ['苏州'], 'time': ['三点', '15:00']},
            },
            {
                'id': 'mixed-01',
                'category': 'mixed',
                'partition': 'calibration',
                'delivery': 'normal',
                'reference': 'Bumblebee tell me the time',
                'critical_fields': {'name': ['Bumblebee']},
            },
        ],
    }


def data():
    return {
        'schema': 1,
        'engine': 'minimax',
        'version': '0.13.1',
        'source': 'synthetic',
        'samples': [
            {
                'id': 'qa-01',
                'condition': 'quiet',
                'distance': '1m',
                'outcome': 'recognized',
                'transcript': '徐州下午三点',
                'fields': {'place': '徐州', 'time': '15:00'},
                'intent_correct': False,
            },
            {
                'id': 'mixed-01',
                'condition': 'tv',
                'distance': '3m',
                'outcome': 'recognized',
                'transcript': 'Bumblebee tell the time',
                'fields': {'name': 'Bumblebee'},
                'intent_correct': True,
            },
        ],
    }


class AsrEvaluationTests(unittest.TestCase):
    def test_shipped_corpus_has_sixty_acceptance_and_separate_calibration_prompts(self):
        samples = corpus_samples(read_json(CORPUS))
        self.assertEqual(len(samples), 75)
        for category in ('qa', 'names_places', 'numbers_time', 'mixed', 'pause_soft'):
            self.assertEqual(
                sum(
                    row['category'] == category and row['partition'] == 'acceptance'
                    for row in samples.values()
                ),
                12,
            )
            self.assertEqual(
                sum(
                    row['category'] == category and row['partition'] == 'calibration'
                    for row in samples.values()
                ),
                3,
            )
        self.assertEqual(len({r['reference'] for r in samples.values()}), 75)

    def test_known_insert_delete_substitute_counts_and_empty_denominator(self):
        self.assertEqual(
            edit_counts('abc', 'axc'),
            {
                'reference_units': 3,
                'errors': 1,
                'substitutions': 1,
                'deletions': 0,
                'insertions': 0,
            },
        )
        self.assertEqual(edit_counts('abc', 'ac')['deletions'], 1)
        self.assertEqual(edit_counts('ac', 'abc')['insertions'], 1)
        self.assertEqual(edit_counts([], ['x'])['insertions'], 1)
        self.assertEqual(edit_counts(['x'], [])['deletions'], 1)
        report = evaluate(
            corpus(),
            {
                'schema': 1,
                'engine': 'minimax',
                'version': '0.13.1',
                'source': 'unspecified',
                'samples': [],
            },
        )
        self.assertTrue(
            all(row['chinese_han_cer']['rate'] is None for row in report['candidate']['groups'])
        )
        self.assertTrue(
            all(
                row['observed'] == 0 and row['missing'] == 1
                for row in report['candidate']['groups']
            )
        )
        self.assertFalse(report['human_acceptance_asserted'])

    def test_edit_distance_invariants_for_small_exhaustive_words(self):
        import itertools

        values = ['', 'a', 'b'] + [''.join(chars) for chars in itertools.product('ab', repeat=3)]
        for left in values:
            for right in values:
                a, b = edit_counts(left, right), edit_counts(right, left)
                self.assertEqual(a['errors'], b['errors'])
                self.assertEqual(a['errors'], a['substitutions'] + a['deletions'] + a['insertions'])
                self.assertGreaterEqual(a['errors'], abs(len(left) - len(right)))
                self.assertLessEqual(a['errors'], max(len(left), len(right)))
                self.assertEqual(edit_counts(left, left)['errors'], 0)

    def test_metrics_and_annotations_stay_partitioned_without_transcript_export(self):
        raw = data()
        raw['samples'][0]['transcript'] += SECRET
        result = evaluate(corpus(), raw)
        row = result['candidate']['cases'][0]
        self.assertEqual(row['chinese_han_cer']['errors'], 1)
        self.assertEqual(row['fields_correct'], 1)
        self.assertFalse(row['critical_sample_exact'])
        english = result['candidate']['cases'][1]['english_wer']
        self.assertEqual(english['reference_units'], 5)
        self.assertEqual(english['deletions'], 1)
        self.assertEqual(result['candidate']['groups'][0]['partition'], 'calibration')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('transcript"', json.dumps(result))
        self.assertFalse(result['contains_content'])
        self.assertEqual(result['candidate']['categories']['acceptance']['qa']['observed'], 1)
        self.assertEqual(result['candidate']['categories']['calibration']['qa']['observed'], 0)

    def test_missing_critical_and_intent_annotations_are_unscored_not_correct(self):
        raw = data()
        raw['samples'][0].pop('fields')
        raw['samples'][0].pop('intent_correct')
        row = evaluate(corpus(), raw)['candidate']['cases'][0]
        self.assertEqual(row['fields_annotated'], 0)
        self.assertFalse(row['critical_sample_exact'])
        group = next(
            g
            for g in evaluate(corpus(), raw)['candidate']['groups']
            if g['partition'] == 'acceptance'
            and g['condition'] == 'quiet'
            and g['distance'] == '1m'
        )
        self.assertIsNone(group['critical_sample_exact_rate'])
        self.assertIsNone(group['intent_accuracy'])
        self.assertEqual(group['fields_expected'], 2)

    def test_numeric_sign_decimal_and_correction_are_not_silently_equivalent(self):
        self.assertNotEqual(normalized('零下-5度'), normalized('零下5度'))
        self.assertNotEqual(normalized('3.05'), normalized('305'))
        self.assertEqual(normalized('Hello，世界！'), normalized('hello 世界'))
        reference, raw = corpus(), data()
        reference['samples'][0]['critical_fields'] = {'number': ['-5.0']}
        raw['samples'][0]['fields'] = {'number': '5.0'}
        result = evaluate(reference, raw)
        self.assertFalse(result['candidate']['cases'][0]['critical_sample_exact'])
        # A transcript containing the right place as a negation is not auto-accepted.
        raw = data()
        raw['samples'][0]['transcript'] = '不是苏州是徐州'
        raw['samples'][0]['fields'] = {'place': '徐州', 'time': '三点'}
        self.assertFalse(evaluate(corpus(), raw)['candidate']['cases'][0]['critical_sample_exact'])

    def test_shipped_wifi_prompt_scores_typographic_word_hyphens_equally(self):
        reference = read_json(CORPUS)
        sample = corpus_samples(reference)['mixed-07']
        for hyphen in ('\u2010', '\u2011'):
            with self.subTest(hyphen=hyphen):
                raw = data()
                raw['samples'] = [
                    {
                        'id': sample['id'],
                        'condition': 'quiet',
                        'distance': '1m',
                        'outcome': 'recognized',
                        'transcript': sample['reference'].replace('-', hyphen),
                        'fields': {'term': 'Bluetooth和Wi' + hyphen + 'Fi'},
                    }
                ]
                row = evaluate(reference, raw)['candidate']['cases'][0]
                self.assertEqual(row['english_wer']['reference_units'], 2)
                self.assertEqual(row['english_wer']['errors'], 0)
                self.assertTrue(row['transcript_exact'])
                self.assertTrue(row['critical_sample_exact'])

        self.assertNotEqual(normalized('Wi-Fi'), normalized('Wi Fi'))
        self.assertNotEqual(normalized('-5.0'), normalized('5.0'))

    def test_failed_capture_counts_full_deletions_not_missing_or_success(self):
        raw = data()
        raw['samples'][0].update(
            outcome='not_detected', transcript='', fields={}, intent_correct=False
        )
        result = evaluate(corpus(), raw)
        row = result['candidate']['cases'][0]
        self.assertEqual(row['chinese_han_cer']['deletions'], 6)
        self.assertFalse(row['transcript_exact'])
        raw['samples'][0]['transcript'] = SECRET
        with self.assertRaises(ValueError):
            evaluate(corpus(), raw)

    def test_ab_requires_same_case_scenario_source_and_annotation_coverage(self):
        original = data()
        candidate = deepcopy(original)
        candidate['samples'][0]['transcript'] = '苏州下午三点'
        candidate['samples'][0]['fields']['place'] = '苏州'
        result = evaluate(corpus(), candidate, original)
        self.assertEqual(result['comparison']['paired_cases'], 2)
        delta = next(
            g
            for g in result['comparison']['deltas_candidate_minus_baseline']
            if g['partition'] == 'acceptance'
            and g['condition'] == 'quiet'
            and g['distance'] == '1m'
        )
        self.assertLess(delta['chinese_han_cer_delta'], 0)
        self.assertEqual(delta['critical_sample_exact_rate_delta'], 1)
        for mutate in [
            lambda d: d['samples'].pop(),
            lambda d: d['samples'][0].update(distance='3m'),
            lambda d: d.update(source='human'),
            lambda d: d['samples'][0]['fields'].pop('place'),
            lambda d: d['samples'][0].pop('intent_correct'),
        ]:
            raw = deepcopy(original)
            mutate(raw)
            with self.assertRaises(ValueError):
                evaluate(corpus(), raw, original)

    def test_pair_counts_detection_failure_as_incorrect_when_candidate_recovers(self):
        baseline = data()
        row = baseline['samples'][0]
        row.update(outcome='not_detected', transcript='', fields={})
        row.pop('intent_correct')
        candidate = data()
        candidate['samples'][0].update(
            transcript='苏州下午三点',
            fields={'place': '苏州', 'time': '15:00'},
            intent_correct=True,
        )
        result = evaluate(corpus(), candidate, baseline)
        before = result['baseline']['cases'][0]
        self.assertFalse(before['critical_sample_annotated'])
        self.assertFalse(before['intent_annotated'])
        self.assertTrue(before['critical_sample_scored'])
        self.assertTrue(before['intent_scored'])
        group = next(
            g for g in result['baseline']['groups'] if g['observed'] and g['distance'] == '1m'
        )
        self.assertEqual(group['critical_sample_exact_rate'], 0)
        self.assertEqual(group['intent_accuracy'], 0)
        delta = next(
            g
            for g in result['comparison']['deltas_candidate_minus_baseline']
            if g['partition'] == 'acceptance'
            and g['condition'] == 'quiet'
            and g['distance'] == '1m'
        )
        self.assertEqual(delta['critical_sample_exact_rate_delta'], 1)
        self.assertEqual(delta['critical_sample_exact_rate_paired_cases'], 1)
        self.assertEqual(delta['intent_accuracy_delta'], 1)
        self.assertEqual(delta['intent_accuracy_paired_cases'], 1)

    def test_pair_with_unannotated_recognition_reports_only_lexical_change(self):
        baseline, candidate = data(), data()
        baseline['samples'][0].update(outcome='failed', transcript='', fields={})
        for sample in (baseline['samples'][0], candidate['samples'][0]):
            sample.pop('intent_correct')
            sample.pop('fields')
        result = evaluate(corpus(), candidate, baseline)
        delta = next(
            g
            for g in result['comparison']['deltas_candidate_minus_baseline']
            if g['partition'] == 'acceptance'
            and g['condition'] == 'quiet'
            and g['distance'] == '1m'
        )
        self.assertLess(delta['chinese_han_cer_delta'], 0)
        for metric in ('critical_sample_exact_rate', 'intent_accuracy'):
            self.assertIsNone(delta[metric + '_delta'])
            self.assertEqual(delta[metric + '_paired_cases'], 0)

    def test_schema_bounds_unknown_keys_duplicates_and_budget_fail_closed(self):
        for mutate in [
            lambda d: d.update(schema=True),
            lambda d: d.update(engine=SECRET),
            lambda d: d['samples'].append(d['samples'][0]),
            lambda d: d['samples'][0].update(id=SECRET),
            lambda d: d['samples'][0].update(transcript='x' * 1001),
            lambda d: d['samples'][0].update(transcript='\ud800'),
            lambda d: d['samples'][0].update(intent_correct=1),
            lambda d: d['samples'][0].update(fields={'private': SECRET}),
            lambda d: d['samples'][0].update(secret=SECRET),
        ]:
            raw = data()
            mutate(raw)
            with self.assertRaises((ValueError, TypeError)):
                evaluate(corpus(), raw)
        with patch('asr_evaluation.MAX_EDIT_CELLS', 0), patch('asr_evaluation.edit_counts') as edit:
            with self.assertRaises(ValueError):
                evaluate(corpus(), data())
            edit.assert_not_called()

    def test_template_is_unmeasured_and_duplicate_unmeasured_rows_are_rejected(self):
        from asr_evaluation import make_template

        template = make_template(read_json(CORPUS))
        self.assertEqual(len(template['samples']), 300)
        report = evaluate(read_json(CORPUS), template)
        self.assertTrue(
            all(
                group['observed'] == 0 and group['chinese_han_cer']['rate'] is None
                for group in report['candidate']['groups']
            )
        )
        self.assertEqual(sum(group['missing'] for group in report['candidate']['groups']), 300)
        template['samples'][1] = deepcopy(template['samples'][0])
        with self.assertRaises(ValueError):
            evaluate(read_json(CORPUS), template)

    def test_private_reader_rejects_permissions_symlink_fifo_and_unsafe_json(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file = root / 'input.json'
            file.write_text('{"schema":1}')
            file.chmod(0o600)
            self.assertEqual(read_json(file, private=True), {'schema': 1})
            file.chmod(0o644)
            with self.assertRaises(ValueError):
                read_json(file, private=True)
            link = root / 'link.json'
            link.symlink_to(file)
            with self.assertRaises(OSError):
                read_json(link, private=True)
            fifo = root / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                read_json(fifo, private=True)
            file.chmod(0o600)
            for contents in ['{"schema":1,"schema":1}', '{"schema":NaN}', ' ' * (512 * 1024 + 1)]:
                file.write_text(contents)
                with self.assertRaises(ValueError):
                    read_json(file, private=True)

    def test_root_cli_minimal_path_scores_and_never_exports_supplied_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            input_file, output = root / 'in.json', root / 'out.json'
            raw = data()
            raw['samples'] = [
                {
                    'id': 'names_places-04',
                    'condition': 'quiet',
                    'distance': '1m',
                    'outcome': 'recognized',
                    'transcript': '杭州西湖' + SECRET,
                    'fields': {'place': '杭州', 'landmark': '西湖'},
                }
            ]
            input_file.write_text(json.dumps(raw))
            input_file.chmod(0o600)
            command = [
                sys.executable,
                str(ROOT / 'cc8'),
                'score-asr',
                '--input',
                str(input_file),
                '--output',
                str(output),
            ]
            process = subprocess.run(
                command,
                cwd='/',
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(process.stdout.strip(), str(output))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(SECRET, output.read_text() + process.stdout + process.stderr)
            saved = output.read_bytes()
            process = subprocess.run(
                command,
                cwd='/',
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(process.returncode, 2)
            self.assertEqual(output.read_bytes(), saved)
            self.assertNotIn(SECRET, process.stderr)
            self.assertFalse(any(p.name.endswith('.tmp') for p in root.iterdir()))


if __name__ == '__main__':
    unittest.main()

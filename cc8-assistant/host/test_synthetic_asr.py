"""Synthetic API checks keep missing coverage, errors and semantics separate."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import synthetic_asr
from synthetic_asr import SyntheticRun, main, pipeline, select_samples


def cases(count=2):
    return [
        dict(
            id=f'qa-{index + 1:02}',
            category='qa',
            partition='calibration',
            delivery='normal',
            reference='你好 Dot',
            critical_fields={},
        )
        for index in range(count)
    ]


class SyntheticASRTests(unittest.TestCase):
    def run_fixture(self, recognize, synthesize=None, count=2):
        return SyntheticRun(
            cases(count), recognize, synthesize or (lambda _: b'\x01\0' * 160)
        ).run()

    def test_real_minimal_path_plan_never_loads_configuration_or_contacts_api(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            absent = Path(folder) / 'not-created'
            result = subprocess.run(
                [sys.executable, str(root / 'cc8'), 'verify-asr', '--runtime-root', str(absent)],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertFalse(plan['execute'])
            self.assertFalse(plan['microphone_used'])
            self.assertEqual(plan['sample_count'], 15)
            self.assertFalse(absent.exists())
        with (
            patch('synthetic_asr.pipeline') as provider,
            patch('synthetic_asr.resolve_voice') as voice,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(['--runtime-root', '/absent']), 0)
            provider.assert_not_called()
            voice.assert_not_called()

    def test_fixed_partitions_and_corpus_identity_cannot_be_silently_relabelled(self):
        self.assertEqual(
            [len(select_samples(p)) for p in ('calibration', 'acceptance', 'all')], [15, 60, 75]
        )
        original = synthetic_asr.read_json(synthetic_asr.CORPUS)
        for kind in ('missing', 'identity', 'category', 'partition'):
            value = copy.deepcopy(original)
            if kind == 'missing':
                value['samples'].pop()
            elif kind == 'identity':
                value['samples'][0]['id'] = 'qa-99'
            elif kind == 'category':
                value['samples'][0]['category'] = 'mixed'
            else:
                value['samples'][0]['partition'] = 'acceptance'
                value['samples'][3]['partition'] = 'calibration'
            with self.subTest(kind=kind), patch('synthetic_asr.read_json', return_value=value):
                with self.assertRaises(ValueError):
                    select_samples('all')

    def test_measurement_completed_does_not_imply_accuracy_or_intent_acceptance(self):
        result = self.run_fixture(lambda _: '错误')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['summary']['normalized_exact_matches'], 0)
        self.assertEqual(result['summary']['han_cer']['errors'], 4)
        for flag in (
            'full_p0_acceptance',
            'intent_assessed',
            'critical_fields_assessed',
            'distance_assessed',
            'television_assessed',
            'microphone_used',
            'device_used',
            'contains_transcript',
        ):
            self.assertFalse(result[flag])
        self.assertNotIn('你好', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('错误', json.dumps(result, ensure_ascii=False))

    def test_empty_transcript_is_a_measured_deletion_not_a_missing_sample(self):
        result = self.run_fixture(lambda _: '', count=1)
        self.assertEqual(result['summary']['measured_samples'], 1)
        self.assertEqual(result['summary']['han_cer']['deletions'], 2)
        self.assertEqual(result['summary']['english_wer']['deletions'], 1)
        self.assertEqual(result['remaining_samples'], 0)

    def test_fixture_failure_never_calls_recognizer_and_stops_after_three_distinct_rows(self):
        for pcm in (b'', b'\0' * 320, b'\x01', b'\x01' * 320002, 'not-bytes'):
            provider = Mock()
            with self.subTest(pcm_type=type(pcm).__name__, size=len(pcm)):
                result = self.run_fixture(provider, lambda _: pcm, count=8)
                provider.assert_not_called()
                self.assertEqual(result['state'], 'partial')
                self.assertEqual(result['processed_samples'], 3)
                self.assertEqual(result['remaining_samples'], 5)
                self.assertTrue(all(r['outcome'] == 'fixture_failed' for r in result['samples']))

    def test_provider_failure_is_redacted_without_retrying_the_same_clip(self):
        number = [0]

        def synthesize(_):
            number[0] += 1
            return bytes((number[0], 0)) * 160

        provider = Mock(side_effect=RuntimeError('PRIVATE_PROVIDER_BODY_AND_KEY'))
        result = self.run_fixture(provider, synthesize, count=8)
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(len({r.args[0] for r in provider.call_args_list}), 3)
        self.assertEqual(result['state'], 'partial')
        self.assertTrue(all(r['outcome'] == 'recognition_failed' for r in result['samples']))
        self.assertNotIn('PRIVATE_PROVIDER', json.dumps(result))

    def test_transcript_bound_prevents_unbounded_scoring(self):
        result = self.run_fixture(lambda _: 'x' * 1001, count=1)
        self.assertEqual(result['samples'][0]['outcome'], 'recognition_failed')
        self.assertEqual(result['summary']['measured_samples'], 0)

    def test_edit_budget_exhaustion_stops_before_another_api_call(self):
        provider = Mock(return_value='你好')
        with patch('synthetic_asr.MAX_EDIT_CELLS', 0):
            result = self.run_fixture(provider)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(result['samples'][0]['outcome'], 'scoring_budget_exceeded')
        self.assertEqual(result['state'], 'partial')

    def test_synthesis_and_recognition_have_separate_monotonic_times(self):
        now = [0.0]

        def synthesize(_):
            now[0] += 2
            return b'\x01\0' * 160

        def recognize(_):
            now[0] += 3
            return '你好 Dot'

        row = SyntheticRun(cases(1), recognize, synthesize, clock=lambda: now[0]).run()['samples'][
            0
        ]
        self.assertEqual(
            (row['synthesis_seconds'], row['recognition_seconds'], row['total_seconds']), (2, 3, 5)
        )

    def test_latency_counts_only_scored_requests_and_reports_missing_coverage(self):
        rows = []
        for duration in (1, 3):
            row = self.run_fixture(lambda _: '你好 Dot', count=1)['samples'][0]
            rows.append({**row, 'recognition_seconds': duration})
        rows.append({'outcome': 'recognition_failed', 'recognition_seconds': 99})
        result = synthetic_asr.totals(rows)
        self.assertEqual(result['recognition_ms'], {'samples': 2, 'p50': 2000, 'p95': 2900})
        self.assertEqual(result['outcomes'], {'recognized': 2, 'recognition_failed': 1})
        self.assertEqual(result['measured_samples'], 2)
        self.assertEqual(synthetic_asr.totals([])['recognition_ms']['p95'], None)

    def test_interrupt_saves_missing_coverage_and_never_continues_uploading(self):
        provider = Mock(side_effect=KeyboardInterrupt)
        saved = []
        result = SyntheticRun(
            cases(4),
            provider,
            lambda _: b'\x01\0' * 160,
            checkpoint=lambda v: saved.append(v['state']),
        ).run()
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['remaining_samples'], 3)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(saved[-1], 'cancelled')

    def test_source_mismatch_and_nonselected_engine_fail_before_credential_constructor(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch('configuration.load_config', return_value=SimpleNamespace(asr='minimax')),
            patch('recognition.MandarinRecognizer') as constructor,
        ):
            with self.assertRaises((OSError, ValueError)):
                pipeline(Path(folder))
            constructor.assert_not_called()
        with (
            patch('configuration.load_config', return_value=SimpleNamespace(asr='whisper')),
            patch('recognition.MandarinRecognizer') as constructor,
        ):
            with self.assertRaises(ValueError):
                pipeline(Path('/absent'))
            constructor.assert_not_called()

    def test_unavailable_voice_rejected_before_credentials_and_private_output(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch('synthetic_asr.platform.system', return_value='Darwin'),
            patch('synthetic_asr.resolve_voice', side_effect=ValueError),
            patch('synthetic_asr.pipeline') as provider,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(['--runtime-root', str(Path(folder).resolve()), '--execute']), 2)
            provider.assert_not_called()
            self.assertFalse(any(Path(folder).iterdir()))

    def test_network_budget_dependency_is_hash_bound_before_provider_creation(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                'configuration.load_config',
                return_value=SimpleNamespace(asr='minimax', asr_model='asr-1.0'),
            ),
            patch('recognition.MandarinRecognizer') as constructor,
        ):
            runtime = Path(folder)
            target = runtime / 'cc8-assistant/host'
            target.mkdir(parents=True)
            for name in synthetic_asr.PIPELINE_FILES:
                if name != 'http_budget.py':
                    (target / name).write_bytes(
                        (synthetic_asr.ROOT / 'cc8-assistant/host' / name).read_bytes()
                    )
            with self.assertRaises(OSError):
                pipeline(runtime)
            constructor.assert_not_called()
            (target / 'http_budget.py').write_bytes(b'changed-network-policy')
            with self.assertRaises(ValueError):
                pipeline(runtime)
            constructor.assert_not_called()
            (target / 'http_budget.py').write_bytes(
                (synthetic_asr.ROOT / 'cc8-assistant/host/http_budget.py').read_bytes()
            )
            _, _, metadata = pipeline(runtime)
            self.assertEqual(set(metadata['pipeline_sha256']), set(synthetic_asr.PIPELINE_FILES))
            constructor.assert_called_once()

    def test_cli_cancellation_closes_provider_and_keeps_report_private(self):
        cancel = Mock()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            private = root / 'device-access/private/voice-runtime'
            private.mkdir(parents=True, mode=0o700)
            private.parent.chmod(0o700)
            with (
                patch('synthetic_asr.ROOT', root),
                patch('synthetic_asr.platform.system', return_value='Darwin'),
                patch('synthetic_asr.resolve_voice', return_value='Tingting'),
                patch(
                    'synthetic_asr.load_release',
                    return_value=SimpleNamespace(backend_version='1.0.0'),
                ),
                patch(
                    'synthetic_asr.pipeline',
                    return_value=(Mock(side_effect=KeyboardInterrupt), cancel, {}),
                ),
                patch('synthetic_asr.synthesize', return_value=b'\x01\0' * 160),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(['--runtime-root', str(root), '--execute']), 130)
            cancel.assert_called_once()
            reports = list((private.parent / 'synthetic-asr').glob('*.json'))
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(reports[0].read_text())['state'], 'cancelled')

    def test_reference_cli_checks_all_pcm_before_provider_and_does_not_resynthesize(self):
        samples = select_samples('calibration')
        first, last, changed = (b'\x01\0', b'\x02\0', b'\x03\0')
        clips = [first] * (len(samples) - 1) + [last]
        baseline = SyntheticRun(samples, lambda _: '你好', Mock(side_effect=clips)).run()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            private = root / 'device-access/private/voice-runtime'
            private.mkdir(parents=True, mode=0o700)
            private.parent.chmod(0o700)
            reference = private.parent / 'reference.json'
            reference.write_text(json.dumps(baseline))
            reference.chmod(0o600)
            args = [
                '--runtime-root',
                str(root),
                '--execute',
                '--reference-report',
                str(reference),
            ]
            report_dir = private.parent / 'synthetic-asr'
            provider, cancel = Mock(return_value='你好'), Mock()
            with (
                patch('synthetic_asr.ROOT', root),
                patch('synthetic_asr.platform.system', return_value='Darwin'),
                patch('synthetic_asr.resolve_voice', return_value='Tingting'),
                patch(
                    'synthetic_asr.load_release',
                    return_value=SimpleNamespace(backend_version='1.0'),
                ),
                patch('synthetic_asr.pipeline', return_value=(provider, cancel, {})) as setup,
                patch('synthetic_asr.synthesize', side_effect=clips[:-1] + [changed]) as synth,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(args), 2)
                self.assertEqual(synth.call_count, len(clips))
                setup.assert_not_called()
                self.assertFalse(list(report_dir.glob('*.json')))
                synth.reset_mock(side_effect=True)
                synth.side_effect = clips
                self.assertEqual(main(args), 0)
                self.assertEqual(synth.call_count, len(clips))
                self.assertEqual([call.args[0] for call in provider.call_args_list], clips)
                cancel.assert_called_once()
                report = json.loads(next(report_dir.glob('*.json')).read_text())
                self.assertTrue(report['reference_fixtures_verified'])

    def test_reference_rejects_changed_corpus_partition_and_sample_identity_before_synthesis(self):
        samples = cases()
        baseline = self.run_fixture(lambda _: '你好')
        synthesize_pcm = Mock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'reference.json'
            for mutation in ('corpus', 'selection', 'duplicate', 'partition', 'missing_hash'):
                with self.subTest(mutation=mutation):
                    report = copy.deepcopy(baseline)
                    if mutation == 'corpus':
                        report['corpus_sha256'] = '0' * 64
                    elif mutation == 'selection':
                        report['selected_samples_sha256'] = '0' * 64
                    elif mutation == 'duplicate':
                        report['samples'][1] = report['samples'][0]
                    elif mutation == 'partition':
                        report['samples'][1]['partition'] = 'acceptance'
                    else:
                        del report['samples'][1]['fixture_pcm_sha256']
                    path.write_text(json.dumps(report))
                    path.chmod(0o600)
                    with self.assertRaises(ValueError):
                        synthetic_asr.reference_fixtures(path, samples, synthesize_pcm)
                    synthesize_pcm.assert_not_called()


if __name__ == '__main__':
    unittest.main()

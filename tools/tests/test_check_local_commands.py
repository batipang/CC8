"""Chinese digital probes distinguish complete results, false hits and missing work."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from tools import check_local_commands as probe


class ChineseKeywordTests(unittest.TestCase):
    def rows(self):
        return [
            dict(
                case=c[0],
                expected=probe.expected(c),
                state='measured',
                **{k: probe.expected(c) for k in probe.CONDITIONS},
            )
            for c in probe.CASES
        ]

    def test_real_minimal_path_plan_has_no_native_import_or_side_effect(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            absent = Path(temporary) / 'absent'
            code = (
                'import runpy,sys; sys.argv='
                + repr([str(root / 'cc8'), 'verify-local-speech', '--model-root', str(absent)])
                + '; result=runpy.run_path(sys.argv[0],run_name="__main__")'
            )
            # Bare Python is intentional: neither numpy nor native models is
            # needed to print the plan. Check native imports in a second probe.
            result = subprocess.run(
                [sys.executable, '-c', code],
                env={'PATH': '/usr/bin:/bin'},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan['sample_count'], 44)
            self.assertEqual(len(plan['conditions']), 6)
            self.assertFalse(plan['execute'])
            self.assertFalse(plan['microphone_used'])
            self.assertFalse(absent.exists())
        code = 'import sys; from tools.check_local_commands import main; main(["--model-root","/absent"]); assert not any(n in sys.modules for n in ("numpy","sherpa_onnx","fast_stop","local_wake"))'
        result = subprocess.run(
            [sys.executable, '-c', code],
            cwd=root,
            env={'PATH': '/usr/bin:/bin'},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixed_corpus_covers_two_real_voices_all_commands_and_cross_talk(self):
        self.assertEqual(len(probe.CASES), len({c[0] for c in probe.CASES}))
        self.assertEqual({c[1] for c in probe.CASES}, {'Tingting', 'Meijia'})
        self.assertEqual({c[5] for c in probe.CASES}, {'', '停一下', '停止', '暂停'})
        self.assertEqual(sum(c[4] for c in probe.CASES), 18)
        self.assertEqual(sum(bool(c[5]) for c in probe.CASES), 24)
        self.assertEqual(sum(c[4] and bool(c[5]) for c in probe.CASES), 6)
        # 大黄风 is acoustically identical to 大黄蜂: never invent a negative
        # that a word spotter cannot distinguish from the target.
        self.assertNotIn('大黄风', {c[3] for c in probe.CASES if not c[4]})

    def test_missing_duplicate_wrong_label_wrong_command_and_partial_cannot_pass(self):
        self.assertTrue(probe.passed(self.rows()))
        for kind in (
            'missing',
            'duplicate',
            'label',
            'wrong_stop',
            'false_wake',
            'failed',
            'partial',
        ):
            rows = self.rows()
            if kind == 'missing':
                rows.pop()
            elif kind == 'duplicate':
                rows[-1] = dict(rows[0])
            elif kind == 'label':
                rows[0]['expected'] = {'wake': [], 'stop': []}
            elif kind == 'wrong_stop':
                rows[0]['start_0ms'] = {'wake': ['大黄蜂'], 'stop': ['停止']}
            elif kind == 'false_wake':
                rows[-1]['start_60ms'] = {'wake': ['大黄蜂'], 'stop': []}
            elif kind == 'failed':
                rows[0]['state'] = 'fixture_or_model_failed'
            else:
                rows[0].pop('start_1000ms')
            with self.subTest(kind=kind):
                self.assertFalse(probe.passed(rows))

    def test_summary_separates_missing_and_false_positive_streams(self):
        rows = self.rows()
        rows[0]['start_0ms'] = {'wake': [], 'stop': []}
        rows[-1]['start_0ms'] = {'wake': ['大黄蜂'], 'stop': ['停止']}
        counts = probe.summary(rows)
        self.assertEqual(
            counts['start_0ms'],
            {
                'measured': 44,
                'exact': 42,
                'wake_misses': 1,
                'wake_false_hits': 1,
                'stop_misses': 0,
                'stop_false_hits': 1,
            },
        )
        rows[1]['state'] = 'fixture_or_model_failed'
        self.assertEqual(probe.summary(rows)['start_0ms']['measured'], 43)

    def test_actual_framing_resets_independent_streams_and_preserves_all_hits(self):
        class Detector:
            def __init__(self, word):
                self.word, self.resets, self.frames = word, 0, []

            def reset(self):
                self.resets += 1

            def feed(self, frame):
                self.frames.append(frame.copy())
                return self.word if np.any(frame) else ''

        wake, stop = Detector('大黄蜂'), Detector('停一下')
        result = probe.measure({'wake': wake, 'stop': stop}, np.ones(480, dtype=np.float32), 960)
        self.assertEqual(result, {'wake': ['大黄蜂'], 'stop': ['停一下']})
        self.assertEqual((wake.resets, stop.resets), (1, 1))
        for detector in (wake, stop):
            combined = np.concatenate(detector.frames)
            self.assertEqual(len(combined), 960 + 480 + 32000)
            self.assertTrue(all(0 < len(frame) <= 320 for frame in detector.frames))
            self.assertFalse(combined[:960].any())
            self.assertTrue(combined[960:1440].all())
            self.assertFalse(combined[1440:].any())

    def test_unverified_models_fail_before_loading_or_synthesizing(self):
        with (
            patch('tools.cc8.verified_models', return_value=False),
            patch.object(probe, 'load_detectors') as load,
            patch.object(probe, 'synthesize') as synth,
        ):
            with self.assertRaises(ValueError):
                probe.evaluate(probe.ROOT, None)
        load.assert_not_called()
        synth.assert_not_called()

    def test_cancel_is_saved_and_not_reported_as_success(self):
        class Output:
            def __init__(self):
                self.states = []

            def write(self, data):
                self.states.append(data['state'])

        output = Output()
        with (
            patch('tools.cc8.verified_models', return_value=True),
            patch.object(probe, 'model_identity', return_value={}),
            patch.object(probe, 'resolve_voice', side_effect=lambda voice: voice),
            patch.object(probe, 'load_detectors', return_value={}),
            patch.object(probe, 'fingerprint', return_value={}),
            patch.object(probe, 'synthesize', side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                probe.evaluate(Path('/unused'), output)
        self.assertEqual(output.states, ['running', 'cancelled'])

    def test_main_explicit_execute_keeps_failure_and_cancellation_exit_codes(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(probe, 'ROOT', Path(temporary).resolve()),
            patch.object(probe.platform, 'system', return_value='Darwin'),
            patch.object(probe, 'evaluate', side_effect=KeyboardInterrupt),
            patch('sys.stderr', new_callable=io.StringIO),
        ):
            (Path(temporary) / 'device-access').mkdir()
            self.assertEqual(
                probe.main(['--model-root', str(Path(temporary).resolve()), '--execute']), 130
            )

    def test_changed_manifest_cannot_redefine_the_trusted_model_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'cc8-assistant/host/resources'
            target.mkdir(parents=True)
            for name in ('fast-stop-model.json', 'english-wake-model.json'):
                (target / name).write_bytes(
                    (probe.ROOT / target.relative_to(root) / name).read_bytes()
                )
            self.assertEqual(len(probe.model_identity(root)), 2)
            (target / 'fast-stop-model.json').write_text('{}')
            with (
                patch('tools.cc8.verified_models') as verify,
                patch.object(probe, 'load_detectors') as load,
            ):
                with self.assertRaises(ValueError):
                    probe.evaluate(root, None)
            verify.assert_not_called()
            load.assert_not_called()

    def test_preflight_error_has_private_valid_aborted_report_and_no_exception_text(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(probe, 'ROOT', Path(temporary).resolve()),
            patch.object(probe.platform, 'system', return_value='Darwin'),
            patch.object(probe, 'evaluate', side_effect=ValueError('secret-sentinel')),
            patch('sys.stderr', new_callable=io.StringIO) as error,
        ):
            root = Path(temporary).resolve()
            (root / 'device-access').mkdir()
            self.assertEqual(probe.main(['--model-root', str(root), '--execute']), 2)
            reports = list((root / 'device-access/private/wake-checks').glob('*.json'))
            self.assertEqual(len(reports), 1)
            data = json.loads(reports[0].read_text())
            self.assertEqual(data['state'], 'aborted')
            self.assertFalse(data['full_p0_acceptance'])
            self.assertNotIn('secret-sentinel', reports[0].read_text() + error.getvalue())


if __name__ == '__main__':
    unittest.main()

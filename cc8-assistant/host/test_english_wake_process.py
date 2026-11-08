"""Bounded real child IPC and fixed lexical decisions, never models or providers."""

from pathlib import Path
import select
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

import numpy as np

from complete_wake_process import CompleteWakeVerifierProcess
from english_wake_process import (
    EnglishWakeVerifierProcess,
    confirms_name,
    OPTIONS,
    load_local_model,
)
import test_complete_wake_process as fixtures


class EnglishProcessTests(unittest.TestCase):
    def test_model_identity_is_fenced_at_load_and_before_and_after_every_decode(self):
        snapshot = Path('/local/verified-model')
        original = (snapshot, {'weights': (1, 2, 3, 4, 5)})
        changed = (snapshot, {'weights': (1, 2, 3, 4, 6)})
        for phase in ('load', 'before', 'during', 'stable'):
            with self.subTest(phase=phase):
                holder = Mock()
                transcribe = Mock(return_value={'text': 'Bumblebee!'})
                sequence = {
                    'load': [original, changed],
                    'before': [original, original, changed],
                    'during': [original, original, original, changed],
                    'stable': [original] * 6,
                }[phase]
                with (
                    patch.dict(
                        'sys.modules',
                        {
                            'mlx': SimpleNamespace(core=SimpleNamespace(float16='float16')),
                            'mlx.core': SimpleNamespace(float16='float16'),
                            'mlx_whisper': SimpleNamespace(transcribe=transcribe),
                            'mlx_whisper.transcribe': SimpleNamespace(ModelHolder=holder),
                        },
                    ),
                    patch(
                        'tool_asr_compare.verified_whisper_snapshot', side_effect=sequence
                    ) as verify,
                ):
                    if phase == 'load':
                        with self.assertRaises(ValueError):
                            load_local_model()
                    else:
                        classify = load_local_model()
                        if phase != 'stable':
                            with self.assertRaises(ValueError):
                                classify(np.zeros(160, dtype=np.float32))
                        else:
                            self.assertTrue(classify(np.zeros(160, dtype=np.float32)))
                            self.assertTrue(classify(np.zeros(160, dtype=np.float32)))
                            self.assertEqual(verify.call_count, 6)
                            self.assertEqual(transcribe.call_count, 2)
                    self.assertEqual(
                        transcribe.call_count,
                        0 if phase in ('load', 'before') else 1 if phase == 'during' else 2,
                    )
                    self.assertNotIn('verify_hashes', verify.call_args_list[0].kwargs)
                    self.assertTrue(
                        all(
                            call.kwargs == {'verify_hashes': False}
                            for call in verify.call_args_list[1:]
                        )
                    )

    def launch(self, reply="s.sendall(b'\\x01')", actual_worker=False):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        script = Path(folder.name) / 'worker.py'
        if actual_worker:
            host = str(Path(__file__).resolve().parent)
            script.write_text(
                'import sys\n'
                f'sys.path.insert(0, {host!r})\n'
                'import english_wake_process as module\n'
                'import numpy as np\n'
                'retained=[]\n'
                'def load():\n'
                ' def classify(samples):\n'
                '  assert all(not np.any(old) for old in retained)\n'
                '  retained.append(samples)\n'
                '  return bool(samples[0]>0)\n'
                ' return classify\n'
                'module.load_local_model=load\n'
                'module._worker(int(sys.argv[1]))\n'
            )
        else:
            script.write_text(fixtures.WORKER.replace('REPLY', reply))
        worker = EnglishWakeVerifierProcess()
        worker._worker_script = script
        self.addCleanup(worker.close)
        with patch.dict('os.environ', {'CC8_MINIMAX_API_KEY': 'test-only-inherited-sentinel'}):
            worker.start()
        self.assertTrue(select.select((worker._channel,), (), (), 3)[0])
        self.assertTrue(worker.ready)
        return worker

    def test_constructor_never_starts_process_or_imports_optional_model(self):
        before = set(sys.modules)
        with patch('wake_verifier_process.subprocess.Popen') as spawn:
            worker = EnglishWakeVerifierProcess()
            self.addCleanup(worker.close)
            self.assertIsNone(worker.confirm(np.zeros(160, dtype=np.float32)))
            spawn.assert_not_called()
        self.assertFalse(
            any(name.startswith(('mlx.', 'mlx_whisper')) for name in set(sys.modules) - before)
        )

    def test_fixed_name_never_rewrites_a_confusable(self):
        for text in ('Bumblebee', 'Bumble bee', 'Hello, BUMBLEBEE!', 'Bumblebee, how are you?'):
            self.assertTrue(confirms_name(text))
        for text in (
            'Humblebee',
            'Bubble bee',
            'Bumble by',
            'Bumbleby',
            'bumblebees',
            'abumblebee',
            '',
            None,
        ):
            self.assertFalse(confirms_name(text))
        self.assertEqual(OPTIONS['language'], 'en')
        self.assertIsNone(OPTIONS['initial_prompt'])
        self.assertFalse(OPTIONS['condition_on_previous_text'])

    def test_binary_name_and_veto_replies_preserve_caller_audio(self):
        for reply, expected in (("s.sendall(b'\\x01')", True), ("s.sendall(b'\\x02')", False)):
            worker = self.launch(reply)
            source = np.ones(1600, dtype=np.float32)
            self.assertIs(worker.confirm(source), expected)
            self.assertTrue(np.all(source == 1))
            self.assertTrue(worker.ready)

    def test_unknown_contradictory_or_invalid_byte_closes_only_its_worker(self):
        for value in (0, 3, 255):
            worker = self.launch(f's.sendall(bytes([{value}]))')
            process = worker._process
            self.assertIsNone(worker.confirm(np.zeros(160, dtype=np.float32)))
            self.assertFalse(worker.ready)
            self.assertIsNotNone(process.wait(timeout=2))

    def test_decode_timeout_reaps_child_and_cannot_reuse_a_late_reply(self):
        worker = self.launch("time.sleep(5);s.sendall(b'\\x01')")
        worker.request_seconds = 0.05
        process = worker._process
        self.assertIsNone(worker.confirm(np.zeros(160, dtype=np.float32)))
        self.assertIsNotNone(process.wait(timeout=2))
        self.assertIsNone(worker.confirm(np.zeros(160, dtype=np.float32)))

    def test_actual_worker_protocol_erases_received_pcm_after_each_verdict(self):
        worker = self.launch(actual_worker=True)
        for value, expected in ((1, True), (-1, False), (1, True)):
            source = np.full(16000, value, dtype=np.float32)
            self.assertIs(worker.confirm(source), expected)
            self.assertTrue(np.all(source == value))

    def test_custom_deadlines_are_bounded_and_legacy_chinese_default_is_unchanged(self):
        for value in (True, 0, -1, float('nan'), float('inf'), 6, 'private'):
            with self.assertRaises(ValueError):
                CompleteWakeVerifierProcess(request_seconds=value)
        self.assertEqual(CompleteWakeVerifierProcess().request_seconds, 1.0)
        self.assertEqual(EnglishWakeVerifierProcess().request_seconds, 2.5)
        self.assertEqual(CompleteWakeVerifierProcess.VALID_REPLIES, frozenset((0, 1, 2, 3)))


if __name__ == '__main__':
    unittest.main()

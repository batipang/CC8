"""The normal backend dispatch preserves an explicit offline speech boundary."""

import argparse
from dataclasses import replace
from pathlib import Path

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app_runtime import run_backend
from configuration import load_config
from speech_providers import LocalSpeech


class LocalSpeechModeTests(unittest.TestCase):
    def test_local_mode_and_legacy_cli_route_without_cloud_or_device(self):
        config = load_config(environ={})
        selected = replace(config, asr='sensevoice', brain='echo')
        for settings, args in ((selected, []), (config, ['--brain', 'echo'])):
            with self.subTest(args=args):
                speech = Mock()
                speech.recognizer.engine = 'sensevoice'
                speech.synthesize.return_value = np.ones(16000, dtype='<i2')
                speech.recognize.return_value = '你好语音链路已准备好'
                dependencies = SimpleNamespace(
                    description='fixture',
                    load_config=lambda: settings,
                    local_speech=Mock(return_value=speech),
                    direct_speech=Mock(),
                    bridge=Mock(),
                    minimax_brain=Mock(),
                    transport_hub=Mock(),
                )
                with (
                    patch('sys.argv', ['bridge', '--speech-smoke', *args]),
                    patch('builtins.print'),
                ):
                    run_backend(dependencies)
                effective = dependencies.local_speech.call_args.kwargs['config']
                self.assertEqual((effective.brain, effective.asr), ('echo', 'sensevoice'))
                speech.recognize.assert_called_once()
                dependencies.direct_speech.assert_not_called()
                dependencies.minimax_brain.assert_not_called()
                dependencies.transport_hub.assert_not_called()

    def test_normal_local_backend_initializes_without_a_cloud_brain(self):
        config = load_config(environ={'CC8_BRAIN_PROVIDER': 'echo'})
        speech = Mock()
        speech.recognizer.engine = 'sensevoice'
        dependencies = SimpleNamespace(
            private=Path('fixture'),
            description='fixture',
            load_config=lambda: config,
            local_speech=Mock(return_value=speech),
            direct_speech=Mock(),
            brain_router=Mock(),
            minimax_brain=Mock(),
            fast_stop=Mock(),
            wake_detector=Mock(),
            workspace=Mock(),
            bridge=Mock(),
            pairing=Mock(side_effect=RuntimeError('fixture stops before transport')),
        )
        with (
            patch('sys.argv', ['bridge']),
            patch('app_runtime.atomic_private_bytes'),
            patch('builtins.print'),
            self.assertRaisesRegex(RuntimeError, 'fixture stops before transport'),
        ):
            run_backend(dependencies)
        self.assertEqual(dependencies.bridge.call_args.args, (speech, None))
        dependencies.direct_speech.assert_not_called()
        dependencies.minimax_brain.assert_not_called()
        dependencies.brain_router.assert_not_called()

    def test_synthesized_pcm_is_not_delivered_after_cancellation(self):
        recognizer = Mock()
        with patch('speech_providers.MandarinRecognizer', return_value=recognizer):
            speech = LocalSpeech(argparse.Namespace(asr='sensevoice'))
        with patch.object(speech, 'synthesize', return_value=np.zeros(16000)) as synthesize:
            self.assertEqual(list(speech.stream('取消', lambda: False)), [])
            synthesize.assert_not_called()
            states = iter([True, False])
            self.assertEqual(list(speech.stream('取消', lambda: next(states))), [])
        speech.cancel()
        recognizer.cancel.assert_called_once()

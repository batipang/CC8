"""Exercise real startup boundaries while providers, devices and model loading stay idle."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge
import service
from brain_router import BrainRouter
from configuration import load_config, write_profile, PROFILE, ConfigError
from minimax_client import MiniMaxBrain, MiniMaxVoice, Transport
from recognition import MandarinRecognizer
from test_minimax import Response, Session

ROOT = Path(__file__).resolve().parents[2]


class ConfigurationIntegrationTests(unittest.TestCase):
    def fixture(self, folder, env=None, **values):
        root = Path(folder)
        defaults = root / 'cc8-assistant/host/voice-config.json'
        defaults.parent.mkdir(parents=True, exist_ok=True)
        defaults.write_text('{"asr":"minimax"}')
        write_profile(root / PROFILE, {'schema_version': 1, **values})
        return load_config(root, env or {})

    def test_provider_parameters_and_explicit_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.fixture(
                folder,
                brain='minimax',
                minimax_model='Example-model',
                tts_voice='Example voice',
                tts_speed=1.2,
                tts_pitch=2,
                tts_emotion='happy',
                connect_timeout=4,
                read_timeout=12,
            )
            transport = Transport(key='test', config=config)
            self.assertEqual(transport.timeout, (4, 12))
            self.assertEqual(transport.base, 'https://api.minimax.cn')
            brain = MiniMaxBrain(transport=transport, config=config)
            self.assertEqual(brain.model, 'Example-model')
            voice = MiniMaxVoice(transport=transport, config=config)
            self.assertEqual(
                (voice.voice, voice.speed, voice.pitch, voice.emotion),
                ('Example voice', 1.2, 2, 'happy'),
            )
            self.assertIsNone(
                MiniMaxVoice(transport=transport, config=config, emotion=None).emotion
            )
            router = BrainRouter(brain, dot=Mock(), selected=config.brain)
            self.assertEqual(router.selected, 'minimax')
            with patch('minimax_client.Transport', return_value=Mock()) as factory:
                recognizer = MandarinRecognizer(config=config)
            self.assertEqual(recognizer.engine, 'minimax')
            factory.assert_called_once_with(config=config)

    def test_launch_agent_persists_only_explicit_validated_nonsecret_overrides(self):
        env = {
            'PATH': '/usr/bin:/bin',
            'CC8_ASR_ENGINE': 'whisper',
            'CC8_TTS_SPEED': '1.20',
            'CC8_MINIMAX_API_KEY': 'PRIVATE-SENTINEL',
            'OTHER_KEY': 'PRIVATE-SECOND',
        }
        with tempfile.TemporaryDirectory() as folder:
            config = self.fixture(folder, env)
            with (
                patch('service.load_config', return_value=config),
                patch.dict(os.environ, env, clear=True),
            ):
                payload = service.agent_payload()
        exported = payload['EnvironmentVariables']
        self.assertEqual(set(exported), {'PATH', 'CC8_ASR_ENGINE', 'CC8_TTS_SPEED'})
        self.assertEqual(exported['CC8_ASR_ENGINE'], 'whisper')
        self.assertEqual(exported['CC8_TTS_SPEED'], '1.2')
        self.assertNotIn('PRIVATE-SENTINEL', str(payload))
        self.assertNotIn('PRIVATE-SECOND', str(payload))

    def test_real_request_encoders_receive_selected_models_voice_and_timeouts(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.fixture(
                folder,
                minimax_base='https://api.minimaxi.com',
                asr_model='asr-test',
                tts_model='speech-test',
                tts_voice='Example voice',
                tts_speed=1.2,
                connect_timeout=4,
            )

            class JsonResponse:
                status_code = 200

                def iter_content(self, **kwargs):
                    yield b'{"text":"test","base_resp":{"status_code":0}}'

                def close(self):
                    pass

            session = Session(JsonResponse())
            transport = Transport(key='test', config=config, session_factory=lambda: session)
            self.assertEqual(transport.transcribe(b'synthetic-test-input'), 'test')
            url, arguments = session.calls[0]
            self.assertEqual(url, 'https://api.minimaxi.com/v1/speech_to_text')
            self.assertEqual(arguments['data']['model'], 'asr-test')
            self.assertEqual(arguments['timeout'], (4, 20))
            session = Session(Response([{'data': {'audio': '0000' * 320, 'status': 2}}]))
            transport = Transport(key='test', config=config, session_factory=lambda: session)
            self.assertTrue(list(MiniMaxVoice(transport, config=config).pcm('synthetic')))
            _, arguments = session.calls[0]
            self.assertEqual(arguments['json']['model'], 'speech-test')
            self.assertEqual(arguments['json']['voice_setting']['voice_id'], 'Example voice')
            self.assertEqual(arguments['json']['voice_setting']['speed'], 1.2)

    def test_bad_startup_configuration_fails_before_any_model_or_provider(self):
        with (
            patch.object(sys, 'argv', ['bridge.py']),
            patch('bridge.load_config', side_effect=ConfigError('asr', 'unsupported_choice')),
            patch('bridge.DirectSpeech') as direct,
            patch('bridge.LocalSpeech') as local,
            patch('sys.stderr', new_callable=io.StringIO) as output,
        ):
            with self.assertRaises(SystemExit) as stopped:
                bridge.run_backend()
        self.assertEqual(stopped.exception.code, 2)
        direct.assert_not_called()
        local.assert_not_called()
        self.assertIn('asr', output.getvalue())

    def test_real_minimal_environment_cli_does_not_disclose_invalid_values(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.fixture(folder)
            write_profile(config.profile, {'schema_version': 1})
            config.profile.write_text('{"schema_version":1,"asr":"PRIVATE-SENTINEL"}')
            result = subprocess.run(
                [sys.executable, str(ROOT / 'cc8-assistant/host/configuration.py'), 'check'],
                env={'PATH': '/usr/bin:/bin', 'CC8_CONFIG_FILE': str(config.profile)},
                capture_output=True,
                text=True,
                timeout=5,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn('asr', result.stderr)
        self.assertNotIn('PRIVATE-SENTINEL', result.stdout + result.stderr)

    def test_both_builds_reject_missing_explicit_signing_reference_before_key_generation(self):
        for project in ('cc8-assistant', 'cc8-diagnostics'):
            with self.subTest(project=project), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config = self.fixture(folder, signing_keystore='missing-original.jks')
                for name in ('configuration.py', 'config_schema.py', 'release_info.py'):
                    shutil.copyfile(
                        ROOT / 'cc8-assistant/host' / name, root / 'cc8-assistant/host' / name
                    )
                shutil.copyfile(ROOT / 'release.json', root / 'release.json')
                script = root / project / 'tools/build.py'
                script.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / project / 'tools/build.py', script)
                paths = root / '.toolchains/cc8/paths.json'
                paths.parent.mkdir(parents=True, exist_ok=True)
                paths.write_text(
                    json.dumps(
                        {
                            'java_home': str(root / 'unused-jdk'),
                            'android_sdk': str(root / 'unused-sdk'),
                            'gradle': str(root / 'unused-gradle'),
                        }
                    )
                )
                result = subprocess.run(
                    [sys.executable, str(script)],
                    env={'PATH': '/usr/bin:/bin'},
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('no replacement key', result.stderr)
                self.assertFalse(config.signing_keystore.exists())
                self.assertFalse(
                    (root / 'cc8-diagnostics/signing/cc8-diagnostics-debug.jks').exists()
                )


if __name__ == '__main__':
    unittest.main()

"""Configuration contracts without credentials, models, capture or service startup."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from configuration import PROFILE, ConfigError, load_config, migrate, reset, write_profile


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.defaults = self.root / 'cc8-assistant/host/voice-config.json'
        self.defaults.parent.mkdir(parents=True)
        self.defaults.write_text('{"asr":"minimax"}')
        self.profile = self.root / PROFILE

    def save(self, **values):
        write_profile(self.profile, {'schema_version': 1, **values})

    def test_priority_sources_and_normalized_numeric_environment(self):
        self.save(asr='whisper', brain='minimax', tts_speed=1.1)
        config = load_config(self.root, {'CC8_ASR_ENGINE': 'sensevoice', 'CC8_TTS_SPEED': '1.20'})
        self.assertEqual(config.asr, 'sensevoice')
        self.assertEqual(config.brain, 'minimax')
        self.assertEqual(config.tts_speed, 1.2)
        self.assertEqual(config.tts_voice, 'Chinese (Mandarin)_Straightforward_Boy')
        self.assertEqual(dict(config.sources)['asr'], 'environment')
        self.assertEqual(dict(config.sources)['brain'], 'private')
        self.assertEqual(dict(config.sources)['tts_voice'], 'default')
        self.assertEqual(load_config(self.root, {}).asr, 'whisper')
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o600)

    def test_wake_window_uses_same_config_priority_and_nonsecret_service_environment(self):
        self.assertEqual(load_config(self.root, {}).wake_window_seconds, 60)
        self.save(wake_window_seconds=30)
        config = load_config(self.root, {'CC8_WAKE_WINDOW_SECONDS': '90'})
        self.assertEqual(config.wake_window_seconds, 90)
        self.assertEqual(dict(config.sources)['wake_window_seconds'], 'environment')
        self.assertEqual(
            config.service_environment(
                {'CC8_WAKE_WINDOW_SECONDS': '90', 'CC8_MINIMAX_API_KEY': 'SECRET'}
            ),
            {'CC8_WAKE_WINDOW_SECONDS': '90'},
        )
        self.assertEqual(load_config(self.root, {}).wake_window_seconds, 30)

    def test_wake_window_rejects_invalid_types_and_bounds_without_values_in_errors(self):
        for value in ('14', '301', '1.5', 'NaN', 'SECRET'):
            with (
                self.subTest(value_type=type(value).__name__),
                self.assertRaises(ConfigError) as failure,
            ):
                load_config(self.root, {'CC8_WAKE_WINDOW_SECONDS': value})
            self.assertNotIn('SECRET', str(failure.exception))
        for value in (True, 60.0, 0, 301):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ConfigError):
                self.save(wake_window_seconds=value)

    def test_chinese_wake_verifier_is_opt_in_and_independent_of_utterance_asr(self):
        self.assertEqual(load_config(self.root, {}).chinese_wake_verifier, 'off')
        env = {'CC8_CHINESE_WAKE_VERIFIER': 'sensevoice'}
        config = load_config(self.root, env)
        self.assertEqual(config.chinese_wake_verifier, 'sensevoice')
        self.assertEqual(config.asr, 'minimax')
        self.assertEqual(config.service_environment(env), env)
        complete = load_config(self.root, {'CC8_CHINESE_WAKE_VERIFIER': 'sensevoice_complete'})
        self.assertEqual(complete.chinese_wake_verifier, 'sensevoice_complete')
        self.assertEqual(complete.asr, 'minimax')
        with self.assertRaises(ConfigError):
            load_config(self.root, {'CC8_CHINESE_WAKE_VERIFIER': 'minimax'})

    def test_paired_capture_is_explicit_and_preserves_config_priority_and_service_export(self):
        self.assertEqual(load_config(self.root, {}).capture_frontend, 'mono')
        self.save(capture_frontend='paired')
        self.assertEqual(load_config(self.root, {}).capture_frontend, 'paired')
        env = {'CC8_CAPTURE_FRONTEND': 'mono', 'CC8_MINIMAX_API_KEY': 'PRIVATE-SENTINEL'}
        config = load_config(self.root, env)
        self.assertEqual(config.capture_frontend, 'mono')
        self.assertEqual(dict(config.sources)['capture_frontend'], 'environment')
        self.assertEqual(config.public_summary()['capture_frontend'], 'mono')
        self.assertEqual(config.service_environment(env), {'CC8_CAPTURE_FRONTEND': 'mono'})
        for value in ('', 'auto', 'PRIVATE-SENTINEL'):
            with self.subTest(value=value), self.assertRaises(ConfigError) as failure:
                load_config(self.root, {'CC8_CAPTURE_FRONTEND': value})
            self.assertNotIn('PRIVATE-SENTINEL', str(failure.exception))

    def test_optional_english_confirmation_preserves_asr_and_service_priority(self):
        self.assertEqual(load_config(self.root, {}).english_wake_verifier, 'off')
        self.save(english_wake_verifier='whisper_complete')
        configured = load_config(self.root, {})
        self.assertEqual(configured.english_wake_verifier, 'whisper_complete')
        self.assertEqual(configured.asr, 'minimax')
        env = {'CC8_ENGLISH_WAKE_VERIFIER': 'off'}
        config = load_config(self.root, env)
        self.assertEqual(config.public_summary()['english_wake_verifier'], 'off')
        self.assertEqual(config.service_environment(env), env)
        with self.assertRaises(ConfigError):
            load_config(self.root, {'CC8_ENGLISH_WAKE_VERIFIER': 'cloud'})

    def test_invalid_fields_do_not_fallback_or_print_values(self):
        cases = (
            {'asr': 'PRIVATE-SENTINEL'},
            {'brain': 'PRIVATE-SENTINEL'},
            {'api_key': 'PRIVATE-SENTINEL'},
            {'tls_port': True},
            {'tls_port': 0},
            {'tts_speed': float('inf')},
            {'tts_pitch': 1.5},
            {'tts_pitch': 10**500},
            {'minimax_base': 'https://PRIVATE-SENTINEL.example'},
            {'credential_file': 'https://PRIVATE-SENTINEL.example'},
            {'tts_voice': 'PRIVATE-SENTINEL\n'},
        )
        for values in cases:
            with self.subTest(fields=list(values)):
                self.profile.parent.mkdir(parents=True, exist_ok=True)
                self.profile.write_text(json.dumps({'schema_version': 1, **values}))
                self.profile.chmod(0o600)
                before = self.profile.read_bytes()
                with self.assertRaises(ConfigError) as caught:
                    load_config(self.root, {})
                self.assertNotIn('PRIVATE-SENTINEL', str(caught.exception))
                self.assertEqual(self.profile.read_bytes(), before)

    def test_environment_types_bounds_and_empty_selector_fail(self):
        for env in (
            {'CC8_TTS_SPEED': 'NaN'},
            {'CC8_TTS_SPEED': 'inf'},
            {'CC8_TTS_PITCH': '1.5'},
            {'CC8_TLS_PORT': 'true'},
            {'CC8_CONNECT_TIMEOUT': '0'},
            {'CC8_READ_TIMEOUT': '121'},
            {'CC8_ASR_ENGINE': ''},
            {'CC8_CONFIG_FILE': ''},
            {'CC8_SIGNING_KEYSTORE': ''},
            {'CC8_MINIMAX_CREDENTIAL_FILE': 'sk-' + 'cp-' + 'test-secret'},
        ):
            with self.subTest(keys=list(env)):
                with self.assertRaises(ConfigError):
                    load_config(self.root, env)

    def test_owner_mode_symlink_and_explicit_missing_file_are_checked(self):
        self.save()
        self.profile.chmod(0o644)
        with self.assertRaisesRegex(ConfigError, 'owner_only'):
            load_config(self.root, {})
        self.profile.chmod(0o600)
        with patch('configuration.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ConfigError, 'owner_only'):
                load_config(self.root, {})
        link = self.root / 'linked.json'
        link.symlink_to(self.profile)
        with self.assertRaisesRegex(ConfigError, 'owner_only'):
            load_config(self.root, {'CC8_CONFIG_FILE': str(link)})
        with self.assertRaisesRegex(ConfigError, 'explicit_file_missing'):
            load_config(self.root, {'CC8_CONFIG_FILE': str(self.root / 'missing.json')})

    def test_corrupt_duplicate_deep_and_oversize_json_are_rejected(self):
        self.save()
        for raw in (
            b'{not-json',
            b'{"schema_version":1,"asr":"minimax","asr":"whisper"}',
            b'[' * 1100 + b'0' + b']' * 1100,
            b' ' * 16385,
            b'{"schema_version":1,"tts_voice":"\\ud800"}',
        ):
            self.profile.write_bytes(raw)
            with self.assertRaises(ConfigError):
                load_config(self.root, {})

    def test_unknown_or_missing_schema_never_silently_migrates(self):
        self.save()
        for data in ({}, {'schema_version': True}, {'schema_version': 2}, {'tts_speed': 1.0}):
            self.profile.write_text(json.dumps(data))
            before = self.profile.read_bytes()
            with self.assertRaises(ConfigError):
                migrate(self.root, {})
            self.assertEqual(self.profile.read_bytes(), before)

    def test_legacy_migration_preserves_choice_without_persisting_environment(self):
        self.save()
        self.profile.write_text('{"asr":"sensevoice"}')
        env = {'CC8_ASR_ENGINE': 'whisper', 'CC8_MINIMAX_API_KEY': 'PRIVATE-SENTINEL'}
        self.assertTrue(load_config(self.root, env).legacy_profile)
        config = migrate(self.root, env)
        self.assertFalse(config.legacy_profile)
        self.assertEqual(config.asr, 'whisper')
        self.assertEqual(
            json.loads(self.profile.read_text()), {'schema_version': 1, 'asr': 'sensevoice'}
        )
        self.assertEqual(load_config(self.root, {}).asr, 'sensevoice')

    def test_reset_keeps_private_backup_and_never_touches_credentials(self):
        self.save(asr='whisper')
        self.profile.write_bytes(b'corrupt PRIVATE-SENTINEL')
        secret = self.root / '.toolchains/cc8/voice-secrets.env'
        secret.write_text('DO-NOT-CHANGE')
        backup = reset(self.root, {})
        self.assertEqual(backup.read_bytes(), b'corrupt PRIVATE-SENTINEL')
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(load_config(self.root, {}).asr, 'minimax')
        self.assertEqual(secret.read_text(), 'DO-NOT-CHANGE')
        self.assertNotIn(
            'PRIVATE-SENTINEL', json.dumps(load_config(self.root, {}).public_summary())
        )

    def test_failed_atomic_write_and_reset_restore_previous_bytes(self):
        self.save(asr='sensevoice')
        before = self.profile.read_bytes()
        with patch('configuration.os.replace', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                self.save(asr='whisper')
        self.assertEqual(self.profile.read_bytes(), before)
        self.assertEqual(list(self.profile.parent.glob('.assistant-*')), [])
        with patch('configuration.write_profile', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                reset(self.root, {})
        self.assertEqual(self.profile.read_bytes(), before)

    def test_external_profile_parent_permissions_are_not_changed(self):
        directory = self.root / 'existing'
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)  # The fixture must remain public even under umask 077.
        path = directory / 'profile.json'
        write_profile(path, {'schema_version': 1})
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_repository_defaults_is_explicit_failure(self):
        self.defaults.unlink()
        with self.assertRaisesRegex(ConfigError, 'repository_defaults_missing'):
            load_config(self.root, {})

    def test_directory_selector_cannot_be_renamed_or_replaced_by_reset(self):
        directory = self.root / 'not-a-config'
        directory.mkdir(mode=0o700)
        content = directory / 'keep.txt'
        content.write_text('keep')
        env = {'CC8_CONFIG_FILE': str(directory)}
        with self.assertRaises(ConfigError):
            load_config(self.root, env)
        with self.assertRaises(ConfigError):
            reset(self.root, env)
        self.assertEqual(content.read_text(), 'keep')

    def test_opened_file_permissions_are_rechecked_after_path_check(self):
        self.save()
        replacement = self.root / 'public-replacement.json'
        replacement.write_text('{"schema_version":1,"asr":"whisper"}')
        replacement.chmod(0o644)
        real_open = os.open

        def replaced(path, flags, *args, **kwargs):
            target = replacement if Path(path) == self.profile else path
            return real_open(target, flags, *args, **kwargs)

        with patch('configuration.os.open', side_effect=replaced):
            with self.assertRaisesRegex(ConfigError, 'owner_only'):
                load_config(self.root, {})


if __name__ == '__main__':
    unittest.main()

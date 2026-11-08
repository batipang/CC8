"""Regression checks for failing closed and isolating optional local resources."""

import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from tools import java_toolchain, quality, test_all


class JavaDiscoveryTests(unittest.TestCase):
    def jdk(self, folder, version=21):
        home = Path(folder) / 'jdk'
        directory = home / 'bin'
        directory.mkdir(parents=True)
        for name in ('java', 'javac'):
            path = directory / name
            path.write_text(f"#!/bin/sh\nprintf '{name} {version}.0.1\\n'\n")
            path.chmod(0o700)
        return home

    def test_java_home_works_without_local_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            home = self.jdk(folder)
            self.assertEqual(
                java_toolchain.require_java(root=Path(folder), env={'JAVA_HOME': str(home)}),
                home / 'bin',
            )

    def test_invalid_explicit_home_does_not_silently_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            home = self.jdk(folder)
            with self.assertRaises(RuntimeError):
                java_toolchain.java_bin(
                    Path(folder), {'JAVA_HOME': '/does-not-exist', 'PATH': str(home / 'bin')}
                )

    def test_config_and_path_fallback_work_without_android_sdk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = self.jdk(folder)
            self.assertEqual(
                java_toolchain.java_bin(root, {'PATH': str(home / 'bin')}), home / 'bin'
            )
            config = root / '.toolchains/cc8/paths.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'java_home': str(home)}))
            self.assertEqual(java_toolchain.java_bin(root, {}), home / 'bin')

    def test_old_jdk_is_an_error_not_a_skip(self):
        with tempfile.TemporaryDirectory() as folder:
            home = self.jdk(folder, 17)
            with self.assertRaisesRegex(RuntimeError, '21'):
                java_toolchain.require_java(root=Path(folder), env={'JAVA_HOME': str(home)})

    def test_missing_java_or_malformed_local_config_is_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(RuntimeError):
                java_toolchain.java_bin(root, {'PATH': ''})
            config = root / '.toolchains/cc8/paths.json'
            config.parent.mkdir(parents=True)
            config.write_text('{bad')
            with self.assertRaises(RuntimeError):
                java_toolchain.java_bin(root, {})
            config.write_text(json.dumps({'java_home': 0}))
            with self.assertRaises(RuntimeError):
                java_toolchain.java_bin(root, {})


class RunnerTests(unittest.TestCase):
    def test_suite_failure_does_not_hide_other_suite_results(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1 if len(calls) == 1 else 0)

        with patch('sys.stdout', new_callable=io.StringIO):
            result = test_all.run_suites(['installer', 'java'], run=run)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 2)

    def test_default_never_enables_private_audio_from_parent_environment(self):
        values = []

        def run(args, **kwargs):
            values.append(kwargs['env']['CC8_TEST_PRIVATE_AUDIO'])
            return subprocess.CompletedProcess(args, 0)

        with (
            patch.dict(os.environ, {'CC8_TEST_PRIVATE_AUDIO': '1'}),
            patch('sys.stdout', new_callable=io.StringIO),
        ):
            self.assertEqual(test_all.run_suites(['host'], run=run), 0)
            self.assertEqual(test_all.run_suites(['host'], private_audio=True, run=run), 0)
        self.assertEqual(values, ['0', '1'])

    def test_timeout_is_failure_and_next_suite_still_runs(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, 1)
            return subprocess.CompletedProcess(args, 0)

        with patch('sys.stdout', new_callable=io.StringIO):
            result = test_all.run_suites(['host', 'java'], run=run)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 2)

    def test_missing_core_dependency_fails_with_setup_instruction(self):
        with (
            patch('tools.test_all.importlib.util.find_spec', return_value=None),
            patch('tools.test_all.require_java') as java,
        ):
            with self.assertRaisesRegex(RuntimeError, 'requirements-dev.txt'):
                test_all.preflight()
        java.assert_not_called()


class FormatterTests(unittest.TestCase):
    def test_partial_primary_body_is_discarded_before_the_second_route(self):
        class Partial(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise urllib.error.HTTPError('https://github.com', 503, 'test only', {}, None)
                return super().read(1)

        with tempfile.TemporaryDirectory() as folder:
            root, manifest, path = self.manifest(folder)
            info = json.loads(manifest.read_text())
            info['github_asset_id'] = 365932021
            manifest.write_text(json.dumps(info))
            with (
                patch.object(quality, 'ROOT', root),
                patch.object(quality, 'MANIFEST', manifest),
                patch(
                    'tools.quality.urllib.request.urlopen',
                    side_effect=[Partial(b'junk'), io.BytesIO(b'jar')],
                ) as network,
                patch('sys.stdout', new_callable=io.StringIO),
            ):
                quality.setup_formatter()
                quality.verified_formatter()
            self.assertEqual(network.call_count, 2)
            self.assertEqual(path.read_bytes(), b'jar')
            self.assertEqual(list(path.parent.glob('.java-format-*')), [])

    def test_official_asset_fallback_is_one_attempt_and_keeps_exact_binary_pin(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                root, manifest, path = self.manifest(folder)
                info = json.loads(manifest.read_text())
                info['github_asset_id'] = 365932021
                manifest.write_text(json.dumps(info))
                error = urllib.error.HTTPError(info['url'], code, 'test gateway', {}, io.BytesIO())
                with (
                    patch.object(quality, 'ROOT', root),
                    patch.object(quality, 'MANIFEST', manifest),
                    patch(
                        'tools.quality.urllib.request.urlopen',
                        side_effect=[error, io.BytesIO(b'jar')],
                    ) as network,
                    patch('sys.stdout', new_callable=io.StringIO),
                ):
                    quality.setup_formatter()
                    quality.verified_formatter()
                    # Subsequent setup uses verified cache, never more routes.
                    quality.setup_formatter()
                self.assertEqual(network.call_count, 2)
                self.assertEqual(network.call_args_list[0].args, (info['url'],))
                request = network.call_args_list[1].args[0]
                self.assertEqual(
                    request.full_url,
                    'https://api.github.com/repos/google/google-java-format/releases/assets/365932021',
                )
                self.assertEqual(request.get_header('Accept'), 'application/octet-stream')
                self.assertTrue(error.closed)
                self.assertTrue(all(c.kwargs == {'timeout': 30} for c in network.call_args_list))
                self.assertEqual(path.read_bytes(), b'jar')
                self.assertEqual(list(path.parent.glob('.java-format-*')), [])

    def test_fallback_never_masks_bad_bytes_client_errors_or_unbounded_retry(self):
        cases = (
            ('bad_hash', io.BytesIO(b'bad'), None),
            ('oversize', io.BytesIO(b'too large'), None),
            ('not_found', 404, None),
            ('forbidden', 403, None),
            ('transport_error', urllib.error.URLError('test only'), None),
            ('api_bad_hash', 504, io.BytesIO(b'bad')),
            ('api_oversize', 504, io.BytesIO(b'too large')),
            ('api_gateway', 504, 504),
        )
        for name, first, second in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as folder:
                root, manifest, path = self.manifest(folder)
                info = json.loads(manifest.read_text())
                info['github_asset_id'] = 365932021
                manifest.write_text(json.dumps(info))
                path.parent.mkdir(parents=True)
                path.write_bytes(b'old corrupt cache')

                def reply(value):
                    return (
                        urllib.error.HTTPError(info['url'], value, 'test only', {}, None)
                        if type(value) is int
                        else value
                    )

                values = [reply(first)] + ([reply(second)] if second is not None else [])
                with (
                    patch.object(quality, 'ROOT', root),
                    patch.object(quality, 'MANIFEST', manifest),
                    patch('tools.quality.urllib.request.urlopen', side_effect=values) as network,
                    patch('sys.stdout', new_callable=io.StringIO),
                ):
                    with self.assertRaises((RuntimeError, urllib.error.URLError)):
                        quality.setup_formatter()
                self.assertEqual(network.call_count, len(values))
                self.assertEqual(path.read_bytes(), b'old corrupt cache')
                self.assertEqual(list(path.parent.glob('.java-format-*')), [])

    def test_invalid_asset_identity_or_nonofficial_primary_fails_before_network(self):
        for value in (True, 0, -1, 2**63, '365932021', 'https://private.invalid'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                root, manifest, _ = self.manifest(folder)
                info = json.loads(manifest.read_text())
                info['github_asset_id'] = value
                manifest.write_text(json.dumps(info))
                with (
                    patch.object(quality, 'ROOT', root),
                    patch.object(quality, 'MANIFEST', manifest),
                    patch('tools.quality.urllib.request.urlopen') as network,
                ):
                    with self.assertRaises(RuntimeError):
                        quality.setup_formatter()
                    network.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            root, manifest, _ = self.manifest(folder)
            info = json.loads(manifest.read_text())
            info.update(github_asset_id=365932021, url='https://other.invalid/formatter.jar')
            manifest.write_text(json.dumps(info))
            with (
                patch.object(quality, 'ROOT', root),
                patch.object(quality, 'MANIFEST', manifest),
                patch('tools.quality.urllib.request.urlopen') as network,
            ):
                with self.assertRaises(RuntimeError):
                    quality.setup_formatter()
                network.assert_not_called()

    def manifest(self, folder, payload=b'jar'):
        root = Path(folder)
        manifest = root / 'formatter.json'
        name = 'google-java-format-1.35.0-all-deps.jar'
        info = {
            'filename': name,
            'version': '1.35.0',
            'url': 'https://github.com/google/google-java-format/releases/download/v1.35.0/' + name,
            'size': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
            'minimum_java': 21,
        }
        manifest.write_text(json.dumps(info))
        return root, manifest, root / '.toolchains/cc8/quality' / name

    def test_missing_or_corrupt_cache_never_downloads_during_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root, manifest, path = self.manifest(folder)
            with (
                patch.object(quality, 'ROOT', root),
                patch.object(quality, 'MANIFEST', manifest),
                patch('tools.quality.urllib.request.urlopen') as network,
            ):
                with self.assertRaises(RuntimeError):
                    quality.verified_formatter()
                path.parent.mkdir(parents=True)
                path.write_bytes(b'corrupt')
                with self.assertRaises(RuntimeError):
                    quality.verified_formatter()
            network.assert_not_called()

    def test_download_verifies_digest_and_cleans_failed_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            root, manifest, path = self.manifest(folder)
            with patch.object(quality, 'ROOT', root), patch.object(quality, 'MANIFEST', manifest):
                with patch('tools.quality.urllib.request.urlopen', return_value=io.BytesIO(b'bad')):
                    with self.assertRaisesRegex(RuntimeError, 'checksum'):
                        quality.setup_formatter()
                self.assertFalse(path.exists())
                self.assertEqual(list(path.parent.glob('.java-format-*')), [])
                with (
                    patch('tools.quality.urllib.request.urlopen', return_value=io.BytesIO(b'jar')),
                    patch('sys.stdout', new_callable=io.StringIO),
                ):
                    quality.setup_formatter()
                quality.verified_formatter()

    def test_oversized_download_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root, manifest, path = self.manifest(folder)
            with (
                patch.object(quality, 'ROOT', root),
                patch.object(quality, 'MANIFEST', manifest),
                patch('tools.quality.urllib.request.urlopen', return_value=io.BytesIO(b'oversize')),
            ):
                with self.assertRaisesRegex(RuntimeError, 'size'):
                    quality.setup_formatter()
            self.assertFalse(path.exists())

    def test_source_discovery_excludes_private_generated_and_downloaded_java(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in (
                'cc8-assistant/app/src/main/A.java',
                'cc8-assistant/app/build/B.java',
                'device-access/private/C.java',
                '.toolchains/D.java',
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertEqual(
                quality.java_sources(root), [str(root / 'cc8-assistant/app/src/main/A.java')]
            )

    def test_check_is_read_only_and_format_formats_before_lint(self):
        run = Mock()
        with (
            patch.object(
                quality,
                'verified_formatter',
                return_value=({'minimum_java': 21}, Path('/tool.jar')),
            ),
            patch.object(quality, 'require_java', return_value=Path('/jdk/bin')),
            patch.object(quality, 'java_sources', return_value=['A.java']),
        ):
            quality.check_or_format('check', run=run)
            commands = [c.args[0] for c in run.call_args_list]
            self.assertFalse(any('--replace' in c or '--fix' in c for c in commands))
            self.assertIn('--set-exit-if-changed', commands[-1])
            run.reset_mock()
            quality.check_or_format('format', run=run)
            self.assertEqual(run.call_args_list[0].args[0][3], 'format')
            commands = [c.args[0] for c in run.call_args_list]
            self.assertEqual(sum('--replace' in c for c in commands), 2)
            self.assertIn('--set-exit-if-changed', commands[-1])

    def test_format_fails_when_java_output_is_not_stable(self):
        def run(command, **kwargs):
            if '--set-exit-if-changed' in command:
                raise subprocess.CalledProcessError(1, command)

        with (
            patch.object(
                quality,
                'verified_formatter',
                return_value=({'minimum_java': 21}, Path('/tool.jar')),
            ),
            patch.object(quality, 'require_java', return_value=Path('/jdk/bin')),
            patch.object(quality, 'java_sources', return_value=['A.java']),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                quality.check_or_format('format', python=False, run=run)


if __name__ == '__main__':
    unittest.main()

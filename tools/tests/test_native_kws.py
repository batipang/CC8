"""Compile actual pinned C++ graph/expression; no model, network or capture."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import native_kws


class NativeKeywordTests(unittest.TestCase):
    def snapshot(self, folder):
        target = Path(folder) / 'snapshot'
        shutil.copytree(native_kws.SNAPSHOT, target)
        return target

    def test_real_cpp_reproduces_before_and_corrects_suffix_confidence(self):
        result = native_kws.verify()
        self.assertEqual(result['state'], 'passed')
        self.assertGreater(result['before']['high_history'], 0.25)
        self.assertLess(result['before']['low_history'], 0.25)
        self.assertLess(result['after']['high_history'], 0.25)
        self.assertGreater(result['after']['low_history'], 0.25)
        self.assertEqual(
            result['before']['no_backoff_control'], result['after']['no_backoff_control']
        )
        for name in (
            'installed_native_tested',
            'runtime_changed',
            'microphone_used',
            'network_requested',
            'full_p0_acceptance',
        ):
            self.assertFalse(result[name])

    def test_exact_source_edits_preserve_explicit_reset_and_untouched_files(self):
        original = native_kws.verified_sources()
        corrected = native_kws.patched_sources()
        self.assertEqual(
            {n for n in original if corrected[n] != original[n]}, set(native_kws.PATCHES)
        )
        explicit = 'void Reset(OnlineStream *s) const override { InitOnlineStream(s); }'
        self.assertIn(explicit, corrected[native_kws.SPOTTER])
        for name in native_kws.PATCHES:
            self.assertTrue(corrected[name].startswith('// Modified by CC8:'))
        self.assertIn(
            'if (trailing_silence > threshold) {\n        ResetKeywordResult(s);',
            corrected[native_kws.SPOTTER],
        )

    def test_actual_reset_members_preserve_encoder_only_for_automatic_reset(self):
        sources = native_kws.patched_sources()
        with tempfile.TemporaryDirectory() as folder:
            result = native_kws.compile_fixture(sources, Path(folder), shutil.which('c++'), 'reset')
        self.assertEqual(
            result,
            {
                'automatic_encoder_preserved': True,
                'explicit_encoder_reset': True,
                'keyword_result_reset': True,
            },
        )

    def test_changed_or_missing_snapshot_fails_before_compile_and_overlay(self):
        for kind in ('changed', 'missing'):
            with tempfile.TemporaryDirectory() as folder:
                root = self.snapshot(folder)
                target = root / native_kws.DECODER
                if kind == 'changed':
                    target.write_bytes(target.read_bytes() + b'changed')
                else:
                    target.unlink()
                output = Path(folder) / 'overlay'
                with self.assertRaises(ValueError), patch.object(subprocess, 'run') as run:
                    native_kws.verify(root)
                run.assert_not_called()
                with self.assertRaises(ValueError):
                    native_kws.write_overlay(output, root)
                self.assertFalse(output.exists())

    def test_links_and_nonregular_source_are_not_read_or_compiled(self):
        for kind in ('symlink', 'hardlink', 'directory', 'fifo'):
            with tempfile.TemporaryDirectory() as folder:
                root = self.snapshot(folder)
                target = root / native_kws.DECODER
                original = native_kws.SNAPSHOT / native_kws.DECODER
                target.unlink()
                if kind == 'symlink':
                    target.symlink_to(original)
                elif kind == 'hardlink':
                    os.link(root / 'LICENSE', target)
                elif kind == 'directory':
                    target.mkdir()
                else:
                    os.mkfifo(target)
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    native_kws.verified_sources(root)

    def test_new_overlay_is_complete_private_and_never_reuses_an_existing_path(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'overlay'
            native_kws.write_overlay(output)
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['state'], 'complete')
            self.assertFalse(manifest['contains_runtime_library'])
            self.assertEqual(set(manifest['files']), set(native_kws.PATCHES) | {'LICENSE'})
            files = {
                p.relative_to(output): p.read_bytes() for p in output.rglob('*') if p.is_file()
            }
            for path in output.rglob('*'):
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600
                )
            with self.assertRaises(FileExistsError):
                native_kws.write_overlay(output)
            self.assertEqual(
                files,
                {p.relative_to(output): p.read_bytes() for p in output.rglob('*') if p.is_file()},
            )

    def test_real_cli_minimal_path_is_source_only(self):
        result = subprocess.run(
            [sys.executable, str(native_kws.ROOT / 'tools/native_kws.py')],
            env={**os.environ, 'PATH': '/usr/bin:/bin'},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value['installed_native_tested'])
        self.assertEqual(value['state'], 'passed')


if __name__ == '__main__':
    unittest.main()

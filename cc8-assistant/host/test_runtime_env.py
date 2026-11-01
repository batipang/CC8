"""Reproduce launchd's missing Node path without any real model/network call."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dot_client import CodexRpc, runtime_status
from runtime_env import command_path, runtime_environment
import service


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_inherited_order_is_preserved_and_paths_are_unique_absolute(self):
        result = command_path('/custom/bin:/usr/bin::.:relative:/custom/bin').split(os.pathsep)
        self.assertEqual(result[:2], ['/custom/bin', '/usr/bin'])
        self.assertEqual(len(result), len(set(result)))
        self.assertTrue(all(os.path.isabs(p) for p in result))

    def test_launchd_minimal_environment_can_execute_node_wrapper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node = root / 'node'
            codex = root / 'codex'
            node.write_text('#!/bin/sh\nprintf "local-node-ok\\n"\n')
            node.chmod(0o700)
            codex.write_text('#!/usr/bin/env node\n')
            codex.chmod(0o700)
            with patch('runtime_env.FALLBACK_DIRS', (folder, '/usr/bin', '/bin')):
                env = runtime_environment({'PATH': ''})
                result = subprocess.run(
                    [str(codex), '--version'], env=env, capture_output=True, text=True, timeout=3
                )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), 'local-node-ok')

    def test_environment_is_copied_without_mutating_owner_settings(self):
        before = {'PATH': '/usr/bin', 'EXISTING_VALUE': 'kept'}
        after = runtime_environment(before)
        self.assertEqual(before, {'PATH': '/usr/bin', 'EXISTING_VALUE': 'kept'})
        self.assertEqual(after['EXISTING_VALUE'], 'kept')
        self.assertIn('/opt/homebrew/bin', after['PATH'])

    def test_agent_persists_only_path_and_no_environment_secret(self):
        with patch.dict(
            os.environ, {'CC8_MINIMAX_API_KEY': 'SENTINEL-SECRET', 'OTHER_KEY': 'SECOND-SECRET'}
        ):
            value = service.agent_payload()
        self.assertEqual(set(value['EnvironmentVariables']), {'PATH'})
        self.assertNotIn('SENTINEL-SECRET', str(value))
        self.assertNotIn('SECOND-SECRET', str(value))

    def test_rpc_child_receives_repaired_path_even_without_service(self):
        seen = {}

        def popen(argv, **kwargs):
            seen.update(kwargs)
            raise OSError('test child not started')

        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}):
            with self.assertRaises(OSError):
                CodexRpc(command=['test-server'], popen=popen).start()
        self.assertIn('/opt/homebrew/bin', seen['env']['PATH'])

    def test_health_reports_missing_node_for_node_based_codex(self):
        with tempfile.TemporaryDirectory() as folder:
            wrapper = Path(folder) / 'codex'
            wrapper.write_text('#!/usr/bin/env node\n')
            with (
                patch('dot_client.codex_command', return_value=[str(wrapper)]),
                patch('dot_client.shutil.which', return_value=None),
            ):
                result = runtime_status()
            self.assertTrue(result['codex_available'])
            self.assertTrue(result['node_required'])
            self.assertFalse(result['node_available'])
            self.assertFalse(result['ready'])

    def test_native_codex_does_not_require_node(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / 'codex'
            binary.write_bytes(b'\xcf\xfa\xed\xfe')
            with (
                patch('dot_client.codex_command', return_value=[str(binary)]),
                patch('dot_client.shutil.which', return_value=None),
            ):
                self.assertTrue(runtime_status()['ready'])


if __name__ == '__main__':
    unittest.main()

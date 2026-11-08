"""Exercise the exact workflow script: wrong downloads cannot reach extraction/PATH."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


def provision_step():
    value = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    steps = value['jobs']['android']['steps']
    matching = [
        step
        for step in steps
        if step.get('name') == 'Provision the exact pinned Gradle distribution'
    ]
    if len(matching) != 1:
        raise AssertionError('Expected exactly one explicit pinned distribution step')
    return steps, matching[0]


class GradleProvisionTests(unittest.TestCase):
    def test_version_digest_and_pipeline_preserve_existing_wrapper_and_build_gates(self):
        steps, step = provision_step()
        properties = dict(
            line.split('=', 1)
            for line in (ROOT / 'cc8-diagnostics/gradle/wrapper/gradle-wrapper.properties')
            .read_text()
            .splitlines()
        )
        version = re.search(r'gradle-([0-9.]+)-bin.zip', properties['distributionUrl'])[1]
        self.assertIn('gradle-' + version + '-bin.zip', step['run'])
        self.assertIn(properties['distributionSha256Sum'], step['run'])
        self.assertEqual(step['shell'], 'bash')
        self.assertIn('--max-time 180 --max-filesize 136920070', step['run'])
        self.assertIn('size != 136920070', step['run'])
        self.assertIn(
            'https://api.github.com/repos/gradle/gradle-distributions/releases/assets/207876967',
            step['run'],
        )
        setup = next(
            s for s in steps if s.get('uses', '').startswith('gradle/actions/setup-gradle@')
        )
        self.assertLess(steps.index(step), steps.index(setup))
        self.assertNotIn('gradle-version', setup['with'])
        self.assertIs(setup['with']['cache-disabled'], True)
        self.assertTrue(
            any(
                'configure-toolchain.py' in s.get('run', '') and 'command -v gradle' in s['run']
                for s in steps
            )
        )
        self.assertTrue(any('tools/build.py' in s.get('run', '') for s in steps))
        self.assertTrue(any('test_apk_signing.py' in s.get('run', '') for s in steps))
        self.assertTrue(any('check-apk' in s.get('run', '') for s in steps))

    def failure(self, body):
        _, step = provision_step()
        with tempfile.TemporaryDirectory(prefix='gradle-workflow-') as folder:
            root = Path(folder)
            temp = root / 'runner temp'
            temp.mkdir()
            bin = root / 'commands with spaces'
            bin.mkdir()
            curl = bin / 'curl'
            curl.write_text('#!/bin/sh\n' + body + '\n')
            curl.chmod(0o700)
            unzip = bin / 'unzip'
            unzip.write_text('#!/bin/sh\nprintf reached > "$RUNNER_TEMP/extraction-reached"\n')
            unzip.chmod(0o700)
            target = root / 'github path'
            target.touch()
            script = root / 'exact-step.sh'
            script.write_text(step['run'])
            env = dict(
                os.environ,
                RUNNER_TEMP=str(temp),
                GITHUB_PATH=str(target),
                PATH=str(bin) + os.pathsep + os.environ['PATH'],
            )
            result = subprocess.run(
                ['bash', str(script)], env=env, capture_output=True, text=True, timeout=10
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((temp / 'extraction-reached').exists())
            self.assertEqual(target.read_text(), '')
            return result

    def test_http_failure_stops_before_any_extraction_or_path_change(self):
        result = self.failure('exit 22')
        self.assertEqual(result.returncode, 22)

    def test_success_http_with_invalid_archive_body_is_rejected_before_unzip(self):
        result = self.failure('printf wrong > "$RUNNER_TEMP/gradle-8.11.1-bin.zip"')
        self.assertIn('size/checksum mismatch', result.stderr)

    def test_missing_download_cannot_expose_any_existing_gradle_as_verified(self):
        result = self.failure('exit 0')
        self.assertIn('No such file', result.stderr)


if __name__ == '__main__':
    unittest.main()

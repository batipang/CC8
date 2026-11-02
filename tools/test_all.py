"""Run all offline suites with development dependencies, not voice models or SDKs."""

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

if __package__:
    from .java_toolchain import require_java
else:
    from java_toolchain import require_java

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    'installer': [
        '-m',
        'unittest',
        'discover',
        '-s',
        'device-access/tests',
        '-p',
        'test_*.py',
        '-v',
    ],
    'host': ['-m', 'unittest', 'discover', '-s', 'cc8-assistant/host', '-p', 'test_*.py', '-v'],
    'tooling': ['-m', 'unittest', 'discover', '-s', 'tools/tests', '-p', 'test_*.py', '-v'],
    'java': ['cc8-diagnostics/tools/test-audio.py'],
}


def preflight():
    if sys.version_info < (3, 11):
        raise RuntimeError('Development tests require Python 3.11+ (3.13 recommended)')
    missing = [
        name
        for name in ('numpy', 'requests', 'webrtcvad', 'hypothesis')
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise RuntimeError(
            'Missing development dependencies: '
            + ', '.join(missing)
            + '; run python -m pip install -r requirements-dev.txt'
        )
    require_java()
    if not shutil.which('openssl'):
        raise RuntimeError('OpenSSL CLI is required for local TLS tests')
    if not shutil.which('c++'):
        raise RuntimeError('C++17 compiler is required for native keyword source regressions')


def run_suites(names, private_audio=False, run=subprocess.run):
    env = dict(os.environ)
    env['CC8_TEST_PRIVATE_AUDIO'] = '1' if private_audio else '0'
    failed = []
    for name in names:
        print(f'\n[{name}] offline checks', flush=True)
        try:
            result = run([sys.executable, *SUITES[name]], cwd=ROOT, env=env, timeout=300)
            if result.returncode:
                failed.append(name)
        except (OSError, subprocess.TimeoutExpired):
            failed.append(name)
            print(f'[{name}] failed to execute or timed out', flush=True)
    print(
        '\nResult: ' + ('FAILED: ' + ', '.join(failed) if failed else 'all suites passed'),
        flush=True,
    )
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=tuple(SUITES), action='append')
    parser.add_argument(
        '--with-private-audio',
        action='store_true',
        help='Explicitly opt into existing private recordings; never records new audio',
    )
    args = parser.parse_args()
    try:
        preflight()
    except RuntimeError as error:
        parser.exit(2, str(error) + '\n')
    return run_suites(args.suite or SUITES, args.with_private_audio)


if __name__ == '__main__':
    raise SystemExit(main())

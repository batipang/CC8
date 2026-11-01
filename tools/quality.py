"""Pinned Python/Java checks; downloads are explicit, checks never fetch tools."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

if __package__:
    from .java_toolchain import require_java
else:
    from java_toolchain import require_java

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tools/resources/google-java-format.json'
FLAGS = [
    '--skip-sorting-imports',
    '--skip-removing-unused-imports',
    '--skip-reflowing-long-strings',
    '--skip-javadoc-formatting',
]
JAVA_ROOTS = (
    'cc8-assistant/app/src',
    'cc8-diagnostics/app/src',
    'cc8-diagnostics/tests',
    'device-access/installer-helper',
)


def java_sources(root=ROOT):
    return sorted(
        str(path) for folder in JAVA_ROOTS for path in (Path(root) / folder).rglob('*.java')
    )


def formatter_info():
    value = json.loads(MANIFEST.read_text())
    if Path(value['filename']).name != value['filename']:
        raise RuntimeError('Invalid formatter filename')
    return value, ROOT / '.toolchains/cc8/quality' / value['filename']


def verified_formatter():
    info, path = formatter_info()
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
        raise RuntimeError('Java formatter missing/corrupt; run python tools/quality.py setup')
    return info, path


def setup_formatter():
    info, path = formatter_info()
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == info['sha256']:
        print('Pinned Java formatter already verified.')
        return
    expected_url = (
        'https://github.com/google/google-java-format/releases/download/v'
        + info['version']
        + '/'
        + info['filename']
    )
    if info['url'] != expected_url:
        raise RuntimeError('Formatter URL must be the pinned official release')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.java-format-', dir=path.parent)
    try:
        with (
            os.fdopen(fd, 'wb') as out,
            urllib.request.urlopen(info['url'], timeout=30) as response,
        ):
            size = 0
            while block := response.read(65536):
                size += len(block)
                if size > info['size']:
                    raise RuntimeError('Formatter download exceeds pinned size')
                out.write(block)
        if (
            size != info['size']
            or hashlib.sha256(Path(temporary).read_bytes()).hexdigest() != info['sha256']
        ):
            raise RuntimeError('Java formatter checksum mismatch')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print('Pinned Java formatter downloaded and SHA-256 verified.')


def check_or_format(action, python=True, java=True, run=subprocess.run):
    if python:
        commands = [
            [sys.executable, '-m', 'ruff', 'check', '.'],
            [
                sys.executable,
                '-m',
                'ruff',
                'format',
                *(['--check'] if action == 'check' else []),
                '.',
            ],
        ]
        if action == 'format':
            commands.reverse()
        for command in commands:
            run(command, cwd=ROOT, check=True)
        run([sys.executable, '-m', 'mypy'], cwd=ROOT, check=True)
    if java:
        info, jar = verified_formatter()
        java_home = require_java(info['minimum_java'])
        sources = java_sources()
        if sources:
            command = [str(java_home / 'java'), '-jar', str(jar), *FLAGS]
            if action == 'format':
                # 1.35.0 can move annotation continuation comments again on a second pass.
                # Bound the passes, then verify stability instead of reporting false success.
                for _ in range(2):
                    run([*command, '--replace', *sources], cwd=ROOT, check=True)
            run([*command, '--dry-run', '--set-exit-if-changed', *sources], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('setup', 'check', 'format'))
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--python-only', action='store_true')
    group.add_argument('--java-only', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'setup':
            setup_formatter()
        else:
            check_or_format(args.action, not args.java_only, not args.python_only)
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()

"""Find the development JDK without requiring private Android SDK configuration."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def _checked_bin(path, source):
    path = Path(path).expanduser()
    if not path.is_absolute() or not all(
        (path / name).is_file() and os.access(path / name, os.X_OK) for name in ('java', 'javac')
    ):
        raise RuntimeError(f'{source} must identify a JDK with executable java and javac')
    return path


def java_bin(root=ROOT, env=None):
    env = os.environ if env is None else env
    for key in ('CC8_TEST_JAVA_HOME', 'JAVA_HOME'):
        if env.get(key):
            return _checked_bin(Path(env[key]) / 'bin', key)
    config = Path(root) / '.toolchains/cc8/paths.json'
    if config.is_file():
        try:
            home = json.loads(config.read_text())['java_home']
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError('Local paths.json has no valid java_home') from error
        if not isinstance(home, str) or not home:
            raise RuntimeError('Local paths.json java_home must be a non-empty path')
        return _checked_bin(Path(home) / 'bin', 'paths.json java_home')
    compiler = shutil.which('javac', path=env.get('PATH', ''))
    if compiler:
        return _checked_bin(Path(compiler).parent, 'PATH')
    raise RuntimeError('JDK 21+ is required; set JAVA_HOME or add java/javac to PATH')


def require_java(minimum=21, root=ROOT, env=None):
    directory = java_bin(root, env)
    for name in ('java', 'javac'):
        try:
            result = subprocess.run(
                [str(directory / name), '-version'], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f'Cannot execute {name}; install JDK {minimum}+') from error
        match = re.search(
            r'(?:javac\s+|version\s+"|(?:openjdk|java)\s+)(\d+)', result.stdout + result.stderr
        )
        if result.returncode or not match or int(match.group(1)) < minimum:
            raise RuntimeError(f'{name} must be JDK {minimum}+; check JAVA_HOME')
    return directory

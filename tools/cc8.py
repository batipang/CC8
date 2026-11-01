"""Prepare, diagnose and start CC8 without hidden capture or a second runtime."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
from configuration import load_config  # noqa: E402
from config_schema import ConfigError  # noqa: E402
from runtime_env import command_path  # noqa: E402
from runtime_guard import (  # noqa: E402
    RuntimeConflict,
    ensure_available,
    other_runtime,
    _private_file,
)

if __package__:
    from .java_toolchain import require_java
else:
    from java_toolchain import require_java


class SetupError(RuntimeError):
    pass


def python_info(python, manifest=None):
    """Inspect package metadata in the selected environment, never import models."""
    code = """import importlib.metadata as m,json,platform,sys
expected=json.loads(sys.argv[1]);problems=[]
for name,version in expected.items():
 try: actual=m.version(name)
 except m.PackageNotFoundError: actual=None
 if actual!=version: problems.append(name)
print(json.dumps(dict(version=list(sys.version_info[:2]),system=platform.system(),
 machine=platform.machine(),missing_or_wrong=problems)))"""
    expected = {}
    if manifest:
        for line in manifest.read_text().splitlines():
            if line.strip() and not line.startswith('#'):
                name, version = line.strip().split('==')
                expected[name] = version
    result = subprocess.run(
        [str(python), '-c', code, json.dumps(expected)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get('missing_or_wrong'), list):
        raise SetupError('Invalid Python environment probe; recreate the selected environment')
    return value


def interpreter(value=None):
    candidate = value or shutil.which('python3.13', path=command_path())
    if not candidate:
        raise SetupError('Install Python 3.13 or pass --python /absolute/path/to/python3.13')
    found = shutil.which(str(candidate), path=command_path())
    if not found:
        raise SetupError('Selected Python is unavailable; pass --python /absolute/path/to/python')
    return Path(found)


def contained_environment(root, name):
    target = root / '.toolchains/cc8' / name
    # Never upgrade an external/symlinked environment shared by the live checkout.
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise SetupError(
            'Environment points outside this checkout; use a separate local environment'
        )
    return target


def setup(args, root=ROOT):
    python = interpreter(args.python)
    profile = args.profile
    info = python_info(python)
    if info['version'] < [3, 11]:
        raise SetupError('Development/setup requires Python 3.11+; Python 3.13 is recommended')
    if profile == 'voice' and (
        info['version'] != [3, 13] or info['system'] != 'Darwin' or info['machine'] != 'arm64'
    ):
        raise SetupError('Voice runtime requires Apple Silicon macOS and Python 3.13')
    if profile == 'android':
        commands = [
            [str(python), str(root / 'cc8-diagnostics/tools/bootstrap-toolchain.py')],
            [str(python), str(root / 'cc8-diagnostics/tools/setup-sdk.py')],
        ]
    elif profile == 'voice':
        contained_environment(root, 'voice-venv')
        commands = [[str(python), str(root / 'scripts/setup-voice.py')]]
        if args.sensevoice:
            commands[0].append('--sensevoice')
    else:
        target = contained_environment(root, 'dev-venv')
        executable = target / 'bin/python'
        commands = []
        if not executable.is_file():
            commands.append([str(python), '-m', 'venv', str(target)])
        else:
            if python_info(executable)['version'] < [3, 11]:
                raise SetupError('Existing dev environment is too old; choose a fresh checkout')
        commands += [
            [str(executable), '-m', 'pip', 'install', '-r', str(root / 'requirements-dev.txt')],
            [str(executable), str(root / 'tools/quality.py'), 'setup'],
        ]
    if args.dry_run:
        # Show the exact argv, never a shell command and never a credential.
        print(json.dumps({'profile': profile, 'commands': commands}, indent=2))
        return 0
    if profile == 'voice':
        ensure_available(root)
        for command in commands:
            subprocess.run(command, cwd=root, check=True)
    else:
        for command in commands:
            subprocess.run(command, cwd=root, check=True)
    print(
        'Setup complete. Run ./cc8 doctor --profile ' + profile + '; no device capture was started.'
    )
    return 0


def verified_models(root, engine):
    def digest(path):
        value = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                value.update(chunk)
        return value.hexdigest()

    resources = root / 'cc8-assistant/host/resources'
    manifest = json.loads((resources / 'fast-stop-model.json').read_text())
    archive = root / '.toolchains/cc8' / manifest['name']
    if not archive.is_file() or digest(archive) != manifest['digest'].split(':')[1]:
        return False
    # Compare extracted files with the pinned archive, not just presence/size.
    with tarfile.open(archive) as source:
        for entry in source.getmembers():
            if not entry.isfile():
                continue
            path = root / '.toolchains/cc8' / entry.name
            if (
                not path.resolve().is_relative_to((root / '.toolchains/cc8').resolve())
                or not path.is_file()
            ):
                return False
            extracted = source.extractfile(entry)
            if extracted is None:
                return False
            with extracted:
                expected = hashlib.sha256()
                for chunk in iter(lambda: extracted.read(1024 * 1024), b''):
                    expected.update(chunk)
            if expected.hexdigest() != digest(path):
                return False
    if engine == 'sensevoice':
        manifest = json.loads((resources / 'sensevoice.json').read_text())
        for name, expected_digest in manifest['sha256'].items():
            path = root / manifest['local_directory'] / name
            if not path.is_file() or digest(path) != expected_digest:
                return False
    elif engine == 'whisper':
        # Legacy Whisper snapshot selection has no pinned content inventory yet.
        # Do not call an unverified cached snapshot ready or download it in doctor.
        return False
    return True


def credential_ready(config, environ):
    value = environ.get('CC8_MINIMAX_API_KEY', '')
    if not value:
        raw = _private_file(config.credential_file, 16384).decode('utf-8')
        for line in raw.splitlines():
            if line.startswith('CC8_MINIMAX_API_KEY='):
                value = line.split('=', 1)[1].strip()
    # Format/readability only, not provider authentication, quota or permissions.
    return value.startswith('sk-cp-')


def dot_ready():
    binary = shutil.which('codex', path=command_path())
    if not binary:
        return False
    with open(binary, 'rb') as source:
        header = source.read(128).split(b'\n', 1)[0]
    return not (header.startswith(b'#!') and b'node' in header) or bool(
        shutil.which('node', path=command_path())
    )


def doctor(profile, root=ROOT):
    checks = []

    def check(name, action, fix, *, required=True):
        try:
            okay = bool(action())
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RuntimeError,
            subprocess.SubprocessError,
            tarfile.TarError,
        ):
            okay = False
        checks.append({'id': name, 'ok': okay, 'required': required, 'fix': '' if okay else fix})

    if profile in ('dev', 'all'):

        def dev():
            value = python_info(
                root / '.toolchains/cc8/dev-venv/bin/python', root / 'requirements-dev.txt'
            )
            return value['version'] >= [3, 11] and not value['missing_or_wrong']

        check('dev_python', dev, './cc8 setup --profile dev')
        check('jdk', lambda: require_java(root=root), 'Install JDK 21+ and set JAVA_HOME')
        check(
            'openssl', lambda: shutil.which('openssl', path=command_path()), 'Install OpenSSL CLI'
        )

        def formatter():
            value = json.loads((root / 'tools/resources/google-java-format.json').read_text())
            path = root / '.toolchains/cc8/quality' / value['filename']
            return (
                path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == value['sha256']
            )

        check('java_formatter', formatter, './cc8 setup --profile dev')
    if profile in ('android', 'all'):

        def android():
            value = json.loads((root / '.toolchains/cc8/paths.json').read_text())
            sdk = Path(value['android_sdk'])
            required = [
                Path(value['gradle']),
                sdk / 'platform-tools/adb',
                sdk / 'platforms/android-35/android.jar',
                sdk / 'build-tools/35.0.0/aapt',
                sdk / 'build-tools/35.0.0/d8',
                sdk / 'build-tools/35.0.0/apksigner',
                sdk / 'build-tools/35.0.0/lib/apksigner.jar',
            ]
            if not all(p.is_file() for p in required):
                return False
            java = require_java(root=root, env={'JAVA_HOME': value['java_home']})
            env = dict(os.environ, JAVA_HOME=str(java.parent), PATH=command_path())
            result = subprocess.run(
                [value['gradle'], '--offline', '--no-daemon', '--version'],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            return bool(re.search(r'^Gradle 8\.11\.1$', result.stdout, re.MULTILINE))

        check(
            'android_toolchain',
            android,
            './cc8 setup --profile android or scripts/configure-toolchain.py',
        )
    if profile in ('voice', 'android', 'all'):
        config = None

        def configuration():
            nonlocal config
            config = load_config(root=root)
            return True

        check(
            'configuration',
            configuration,
            'python3 cc8-assistant/host/configuration.py check; see docs/CONFIGURATION.md',
        )
        if config:
            if profile in ('android', 'all'):
                key = (
                    config.signing_keystore
                    or root / 'cc8-diagnostics/signing/cc8-diagnostics-debug.jks'
                )
                check(
                    'signing_reference',
                    key.is_file,
                    'Import the original signing keystore before updating an installed app; see docs/DEVELOPMENT.md',
                    required=config.signing_keystore is not None,
                )
            if profile in ('voice', 'all'):

                def voice():
                    value = python_info(
                        root / '.toolchains/cc8/voice-venv/bin/python',
                        root / 'cc8-assistant/host/requirements-runtime.txt',
                    )
                    return (
                        value['version'] == [3, 13]
                        and value['system'] == 'Darwin'
                        and value['machine'] == 'arm64'
                        and not value['missing_or_wrong']
                    )

                check(
                    'voice_python',
                    voice,
                    './cc8 setup --profile voice (stop the current runtime first)',
                )
                check(
                    'models',
                    lambda: verified_models(root, config.asr),
                    './cc8 setup --profile voice'
                    + (' --sensevoice' if config.asr == 'sensevoice' else '')
                    + '; Whisper needs a separately verified snapshot',
                )
                check(
                    'minimax_credential_format',
                    lambda: credential_ready(config, os.environ),
                    'Configure owner-only MiniMax credential reference; see docs/CONFIGURATION.md',
                )
                if config.brain == 'dot':
                    check(
                        'dot_tools',
                        dot_ready,
                        'Install owner-configured Codex CLI and its required Node runtime',
                    )
        if profile in ('voice', 'all'):
            check(
                'runtime_ownership',
                lambda: not other_runtime(root),
                'Stop the other checkout from its own directory first',
            )

            def health():
                from diagnose import query

                result = query(
                    path=root / 'device-access/private/voice-runtime/announce.sock',
                    operation='health',
                )
                return all(
                    result.get(k) is True
                    for k in ('model_ready', 'recognition_worker', 'response_worker')
                )

            check(
                'backend_health',
                health,
                './cc8 run; this is optional before first startup',
                required=False,
            )

            def connection():
                from diagnose import query

                value = query(
                    path=root / 'device-access/private/voice-runtime/announce.sock',
                    operation='health',
                )
                return value.get('connected') is True

            check(
                'authenticated_device',
                connection,
                'Connect USB or open the assistant on the paired Wi-Fi; no capture was started',
                required=False,
            )
    return {
        'schema_version': 1,
        'profile': profile,
        'ok': all(c['ok'] for c in checks if c['required']),
        'checks': checks,
        'network_request_tested': False,
        'device_capture_started': False,
    }


def run_runtime(args, root=ROOT):
    python = root / '.toolchains/cc8/voice-venv/bin/python'
    if args.stop:
        command = [str(python), str(root / 'cc8-assistant/host/service.py'), 'stop']
    else:
        report = doctor('voice', root)
        if not report['ok']:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        if args.foreground:
            command = [str(python), '-u', str(root / 'cc8-assistant/host/bridge.py')]
        else:
            if platform.system() != 'Darwin':
                raise SetupError(
                    'Login service is macOS-only; use --foreground on a supported voice platform'
                )
            command = [str(python), str(root / 'cc8-assistant/host/service.py'), 'start']
    # No launcher/UI call: starting/stopping a host service never starts capture.
    return subprocess.run(command, cwd=root).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser(
        'setup', help='Explicitly download/install one isolated tool profile'
    )
    prepare.add_argument('--profile', choices=('dev', 'android', 'voice'), default='dev')
    prepare.add_argument('--python', help='Python executable (3.13 recommended)')
    prepare.add_argument('--sensevoice', action='store_true')
    prepare.add_argument('--dry-run', action='store_true')
    inspect = commands.add_parser('doctor', help='Read-only dependency/configuration/status checks')
    inspect.add_argument('--profile', choices=('dev', 'android', 'voice', 'all'), default='all')
    inspect.add_argument('--json', action='store_true')
    start = commands.add_parser(
        'run', help='Start/reuse the host only; never opens CC8 UI/microphone'
    )
    group = start.add_mutually_exclusive_group()
    group.add_argument('--foreground', action='store_true')
    group.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'setup':
            if args.sensevoice and args.profile != 'voice':
                parser.error('--sensevoice requires --profile voice')
            return setup(args)
        if args.command == 'run':
            return run_runtime(args)
        result = doctor(args.profile)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for item in result['checks']:
                label = 'OK' if item['ok'] else ('FAIL' if item['required'] else 'INFO')
                print(f'[{label}] {item["id"]}' + (': ' + item['fix'] if item['fix'] else ''))
            print('Local readiness only; no API request or device capture was performed.')
        return 0 if result['ok'] else 2
    except (ConfigError, RuntimeConflict, SetupError) as error:
        parser.exit(2, str(error) + '\n')
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(
            2,
            'Local command failed; run ./cc8 doctor for the relevant profile (no secret values shown)\n',
        )


if __name__ == '__main__':
    raise SystemExit(main())

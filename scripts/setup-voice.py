"""Prepare Apple Silicon / Python 3.13 voice dependencies and verified models.

Does not read credentials, connect to a device, start the service, or record.
"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / '.toolchains/cc8'
RESOURCES = ROOT / 'cc8-assistant/host/resources'
sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
from runtime_guard import RuntimeLease, RuntimeConflict  # noqa: E402


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def download(url, destination, expected):
    if destination.is_file() and digest(destination) == expected:
        return
    partial = destination.with_suffix(destination.suffix + '.partial')
    subprocess.run(
        [
            'curl',
            '-fLsS',
            '--retry',
            '2',
            '--connect-timeout',
            '20',
            '--max-time',
            '600',
            '-o',
            str(partial),
            url,
        ],
        check=True,
    )
    if digest(partial) != expected:
        partial.unlink()
        raise RuntimeError('Checksum mismatch: ' + destination.name)
    partial.replace(destination)


def prepare_models(sensevoice=False):
    manifest = json.loads((RESOURCES / 'fast-stop-model.json').read_text())
    archive = TOOLS / manifest['name']
    download(manifest['browser_download_url'], archive, manifest['digest'].split(':', 1)[1])
    # Extract only the expected single top-level model directory; reject links.
    expected_root = manifest['name'].removesuffix('.tar.bz2')
    with tarfile.open(archive, 'r:bz2') as source:
        members = source.getmembers()
        for member in members:
            relative = Path(member.name)
            target = (TOOLS / relative).resolve()
            if (
                relative.is_absolute()
                or not relative.parts
                or relative.parts[0] != expected_root
                or not target.is_relative_to(TOOLS.resolve())
                or not (member.isfile() or member.isdir())
            ):
                raise RuntimeError('Unsafe model archive entry')
        source.extractall(TOOLS, members=members, filter='data')
    if sensevoice:
        manifest = json.loads((RESOURCES / 'sensevoice.json').read_text())
        destination = ROOT / manifest['local_directory']
        destination.mkdir(parents=True, exist_ok=True)
        for name, expected in manifest['sha256'].items():
            url = (
                'https://huggingface.co/'
                + manifest['repo']
                + '/resolve/'
                + manifest['revision']
                + '/'
                + name
            )
            download(url, destination / name, expected)


def prepare(sensevoice=False):
    if (
        sys.version_info[:2] != (3, 13)
        or platform.system() != 'Darwin'
        or platform.machine() != 'arm64'
    ):
        raise RuntimeError('This pinned runtime requires CPython 3.13 on Apple Silicon macOS')
    TOOLS.mkdir(parents=True, exist_ok=True)
    environment = TOOLS / 'voice-venv'
    if environment.is_symlink() or not environment.resolve().is_relative_to(ROOT.resolve()):
        raise RuntimeError(
            'Voice environment must be local to this checkout, not an external symlink'
        )
    if not (environment / 'bin/python').is_file():
        venv.create(environment, with_pip=True)
    python = environment / 'bin/python'
    manifest = json.loads((RESOURCES / 'webrtc-audio.json').read_text())
    # Verify the native AEC wheel before installing it, including on repeated setup.
    with tempfile.TemporaryDirectory(prefix='cc8-aec-wheel-') as folder:
        subprocess.run(
            [
                str(python),
                '-m',
                'pip',
                'download',
                '--only-binary=:all:',
                '--no-deps',
                '--dest',
                folder,
                manifest['package'] + '==' + manifest['version'],
            ],
            check=True,
        )
        wheel = Path(folder) / manifest['wheel']
        if not wheel.is_file() or digest(wheel) != manifest['sha256']:
            raise RuntimeError('AEC wheel checksum / platform mismatch')
        subprocess.run([str(python), '-m', 'pip', 'install', '--no-deps', str(wheel)], check=True)
    subprocess.run(
        [
            str(python),
            '-m',
            'pip',
            'install',
            '-r',
            str(ROOT / 'cc8-assistant/host/requirements-runtime.lock'),
        ],
        check=True,
    )
    prepare_models(sensevoice)
    print(
        'Voice environment ready. Configure MiniMax credentials and Codex CLI, then run launch.py.'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--sensevoice', action='store_true', help='Also download optional local ASR'
    )
    args = parser.parse_args()
    try:
        # Direct execution uses the same guard as the unified CLI. Hold it through
        # pip/model preparation so no cooperating worker can load partial updates.
        with RuntimeLease(ROOT):
            prepare(args.sensevoice)
    except BlockingIOError:
        parser.exit(
            2, 'Another CC8 runtime or setup owns the lease; stop it before changing dependencies\n'
        )
    except (RuntimeError, RuntimeConflict) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
